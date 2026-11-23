"""The interactive CLI must resume only the original Gemini session after a lost SSH process."""

import json
import sys
import time

import pytest

from ccfleet_agent import gemini_editor_client as terminal
from ccfleet_agent import project_files, workspace_tools

# Each process is a replacement for one SSH attachment to the same paired slot. The slot
# records actual wire requests before replying, including anything sent on rejected handshakes.
PEER = """
import json, signal, struct, sys
from pathlib import Path

signal.alarm(8)
root = Path(sys.argv[1])
index_path = root / "index"
index = int(index_path.read_text()) if index_path.exists() else 0
index_path.write_text(str(index + 1))
attachments = json.loads((root / "attachments").read_text())
def read():
    header = sys.stdin.buffer.read(4)
    if not header:
        raise SystemExit
    size, = struct.unpack(">I", header)
    message = json.loads(sys.stdin.buffer.read(size))
    with (root / "requests").open("a") as f:
        print(json.dumps(message), file=f)
    return message
def send(**message):
    data = json.dumps(message).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(data)) + data)
    sys.stdout.buffer.flush()
def ready(**overrides):
    frame = {"type": "ready", "model": "gemini-high", "models": ["gemini-high", "gemini-low"],
             "capabilities": {"resumeSession": True, "cancelTurn": True}, "resumeToken": "ab"*32}
    send(**dict(frame, **overrides))
def prompt():
    message = read()
    assert message["type"] == "prompt", message["type"]
    return message
def tool(n, op, **fields):
    send(type="request", id=n, op=op, **fields)
    return read()
def end(turn):
    send(type="end", reason="end_turn", turn=turn["turn"])
def drain():
    while read()["type"] not in ("close", "prompt"):
        pass
try:
    if index >= len(attachments):
        drain()
        raise RuntimeError("Unexpected slot attachment")
    exec(attachments[index])
except BrokenPipeError:
    raise SystemExit
except Exception as exc:
    with (root / "errors").open("a") as f:
        print(type(exc).__name__, str(exc), file=f)
    raise
"""

OPEN = '{"type": "open", "version": 1}'
RESUME = '{"type": "resume", "version": 1, "resumeToken": "ab"*32}'
NEW = f'assert read() == {OPEN}\nready()\nassert read() == {{"type": "enableResume"}}\n'
RESTORE = f'assert read() == {RESUME}\nready(resumed=True)\nassert read() == {{"type": "enableResume"}}\n'


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "client folder"
    folder.mkdir()
    return folder


def exercise(tmp_path, project, monkeypatch, inputs, attachments, *, allow_rejection=False):
    (tmp_path / "attachments").write_text(json.dumps(attachments))
    answers = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    try:
        terminal.run(project, [sys.executable, "-I", "-c", PEER, str(tmp_path)], {},
                     workspace_tools, project_files, fleet_home=tmp_path / "fleet")
    except ValueError:
        if not allow_rejection:
            raise
    else:
        assert next(answers, None) is None
    failures = tmp_path / "errors"
    assert not failures.exists(), failures.read_text() if failures.exists() else ""
    return [json.loads(row) for row in (tmp_path / "requests").read_text().splitlines()]


def prompts(requests):
    return [r["text"] for r in requests if r["type"] == "prompt"]


def test_resume_keeps_context_and_drops_pending_write_and_read_versions(
        tmp_path, project, monkeypatch, capsys):
    (project / "notes").write_text("earlier revision")
    decisions = []

    def accept(question):
        decisions.append(question)
        return True

    monkeypatch.setattr(terminal, "confirm", accept)
    # An edit is approved but not written before disconnect. The reopened slot must
    # independently read file state and get a fresh one-shot approval.
    requests = exercise(tmp_path, project, monkeypatch, ["first", "follow-up", "/exit"], [
        NEW + """
first = prompt()
send(type="progress", turn=first["turn"])
send(type="text", text="partial reply")
assert tool(1, "read", path="notes")["result"] == "earlier revision"
assert tool(2, "approve", path="notes", content="interrupted change")["result"] is True
""",
        RESTORE + """
second = prompt()
assert "error" in tool(1, "write", path="notes", content="interrupted change")
assert "error" in tool(2, "approve", path="notes", content="new change")
assert tool(3, "read", path="notes")["result"] == "earlier revision"
assert tool(4, "approve", path="notes", content="new change")["result"] is True
assert tool(5, "write", path="notes", content="new change")["result"] is True
send(type="text", text="resumed reply")
end(second)
assert read() == {"type": "close"}
""",
    ])
    assert prompts(requests) == ["first", "follow-up"]
    assert sum(r["type"] == "open" for r in requests) == 1
    assert len(decisions) == 2 and (project / "notes").read_text() == "new change"
    output = capsys.readouterr().out
    assert "partial reply" in output and "resumed reply" in output
    assert "interrupt" in output.lower() and "ab" * 32 not in output


def test_expired_context_requires_explicit_reconnect_and_new_instruction(
        tmp_path, project, monkeypatch, capsys):
    requests = exercise(tmp_path, project, monkeypatch, [
        "interrupted", "never send A", "never send B", "/reconnect", "user chose fresh", "/exit",
    ], [
        NEW + "prompt()",
        f'assert read() == {RESUME}\nsend(type="error", message="The previous Gemini session cannot be resumed.")',
        NEW + 'turn = prompt()\nend(turn)\nassert read() == {"type": "close"}',
    ])
    assert prompts(requests) == ["interrupted", "user chose fresh"]
    assert [r["type"] for r in requests if r["type"] in ("open", "resume")] == ["open", "resume", "open"]
    output = capsys.readouterr().out.lower()
    assert "/reconnect" in output and "fresh" in output


def test_temporary_failure_retains_handle_and_does_not_retry_uncertain_prompts(
        tmp_path, project, monkeypatch):
    requests = exercise(tmp_path, project, monkeypatch, [
        "interrupted", "/reconnect", "/reconnect", "sent once", "/exit",
    ], [
        NEW + "prompt()",
        f'assert read() == {RESUME}',  # transport breaks during the handshake itself
        RESTORE + 'turn = prompt()\nend(turn)\nassert read() == {"type": "close"}',
    ])
    assert prompts(requests) == ["interrupted", "sent once"]
    assert [r["type"] for r in requests if r["type"] in ("open", "resume")] == ["open", "resume", "resume"]


def test_busy_detaching_worker_is_retried_before_sending_followup(tmp_path, project, monkeypatch):
    requests = exercise(tmp_path, project, monkeypatch, ["interrupted", "next", "/exit"], [
        NEW + "prompt()",
        f'assert read() == {RESUME}\nsend(type="error", message="Another Gemini editor is using this slot. Close it and reconnect.")',
        RESTORE + 'end(prompt())\nassert read() == {"type": "close"}',
    ])
    assert prompts(requests) == ["interrupted", "next"]
    assert sum(r["type"] == "resume" for r in requests) == 2


@pytest.mark.parametrize("reply", [
    'ready(resumed=False)',
    'ready(resumed=True, resumeToken="cd"*32)',
    'ready(resumed=True, model="gemini-low")',
])
def test_unproven_resume_cannot_receive_the_next_instruction(tmp_path, project, monkeypatch, reply):
    # A fatal rejection may close the UI or return to its prompt for explicit action.
    requests = exercise(tmp_path, project, monkeypatch, ["interrupted", "keep this local", "/exit"], [
        NEW + "prompt()", f'assert read() == {RESUME}\n{reply}\ndrain()',
    ], allow_rejection=True)
    assert [r["type"] for r in requests if r["type"] in ("open", "resume")] == ["open", "resume"]
    assert prompts(requests) == ["interrupted"]


def test_legacy_slot_uses_original_prompt_frame(tmp_path, project, monkeypatch):
    requests = exercise(tmp_path, project, monkeypatch, ["legacy", "/exit"], [
        f'assert read() == {OPEN}\nsend(type="ready", model="gemini-old")\n'
        'assert prompt() == {"type": "prompt", "text": "legacy"}\n'
        'send(type="end", reason="end_turn")\nassert read() == {"type": "close"}'
    ])
    assert [r["type"] for r in requests] == ["open", "prompt", "close"]


def test_legacy_slot_cannot_silently_start_a_new_conversation_after_a_drop(
        tmp_path, project, monkeypatch):
    legacy_ready = f'assert read() == {OPEN}\nsend(type="ready", model="gemini-old")\n'
    requests = exercise(tmp_path, project, monkeypatch, [
        "first", "needs previous history", "/reconnect", "new session", "/exit",
    ], [
        legacy_ready + "prompt()",
        legacy_ready + 'prompt()\nsend(type="end", reason="end_turn")\nassert read() == {"type": "close"}',
    ])
    assert prompts(requests) == ["first", "new session"]
    assert [r["type"] for r in requests] == ["open", "prompt", "open", "prompt", "close"]


def test_resume_preserves_the_model_selected_by_the_user(tmp_path, project, monkeypatch):
    requests = exercise(tmp_path, project, monkeypatch, [
        "/model gemini-low", "first", "/reconnect", "follow-up", "/exit",
    ], [
        f'assert read() == {OPEN}\n' + """
ready()
assert read() == {"type": "set_model", "model": "gemini-low"}
send(type="model", model="gemini-low")
assert read() == {"type": "enableResume"}
prompt()
""",
        f'assert read() == {RESUME}\n' + """
ready(resumed=True, model="gemini-low")
assert read() == {"type": "enableResume"}
end(prompt())
assert read() == {"type": "close"}
""",
    ])
    assert prompts(requests) == ["first", "follow-up"]
    assert sum(r["type"] == "set_model" for r in requests) == 1


def test_reconnect_without_a_model_turn_does_not_enroll_a_native_hold(tmp_path, project, monkeypatch):
    requests = exercise(tmp_path, project, monkeypatch, ["/reconnect", "/help", "/exit"], [
        f'assert read() == {OPEN}\nready()\nassert read() == {{"type": "close"}}',
    ])
    assert [r["type"] for r in requests] == ["open", "close"]


@pytest.mark.parametrize("bad", [
    'send(type="progress", turn=turn["turn"]+1)',
    'send(type="progress", turn=True)',
    'send(type="end", reason="end_turn", turn=turn["turn"]+1)',
])
def test_bad_turn_tags_cannot_release_untrusted_output(tmp_path, project, monkeypatch, capsys, bad):
    first = NEW + f'turn = prompt()\n{bad}\nsend(type="text", text="ILLEGAL TRAILING TEXT")\nend(turn)\ndrain()'
    requests = exercise(tmp_path, project, monkeypatch, ["valid", "withhold", "/exit"], [first],
                        allow_rejection=True)
    assert prompts(requests) == ["valid"]
    assert "ILLEGAL TRAILING TEXT" not in capsys.readouterr().out


def test_late_confirmation_on_dead_transport_cannot_create_a_directory(
        tmp_path, project, monkeypatch):
    def confirm_after_eof(_):
        (tmp_path / "approval-visible").touch()
        deadline = time.monotonic() + 5
        while not (tmp_path / "dropped").exists():
            assert time.monotonic() < deadline, "test peer never closed its stdout"
            time.sleep(0.01)
        return True

    monkeypatch.setattr(terminal, "confirm", confirm_after_eof)
    requests = exercise(tmp_path, project, monkeypatch, ["make directory", "yes", "continue", "/exit"], [
        NEW + """
import os, time
prompt()
send(type="request", id=1, op="mkdir", path="stale-directory")
deadline = time.monotonic() + 5
while not (root / "approval-visible").exists():
    assert time.monotonic() < deadline, "terminal never displayed an approval"
    time.sleep(0.01)
os.close(1)
(root / "dropped").touch()
""",
        RESTORE + 'end(prompt())\nassert read() == {"type": "close"}',
    ])
    assert prompts(requests) == ["make directory", "continue"]
    assert not (project / "stale-directory").exists()
