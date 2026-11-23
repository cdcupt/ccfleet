"""The T3 stdio provider retains the workspace and approvals on the connecting Mac."""

import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading

import pytest

from ccfleet_agent import gemini_acp_client as acp
from ccfleet_agent import gemini_editor_client as editor
from ccfleet_agent import project_files, workspace_tools

SLOT = '''
import json,os,struct,sys,time
from pathlib import Path
log=Path(sys.argv[1]);run=Path(sys.argv[2])
def get():
    h=sys.stdin.buffer.read(4)
    if not h:sys.exit()
    size,=struct.unpack(">I",h)
    msg=json.loads(sys.stdin.buffer.read(size))
    with log.open("a") as f:f.write(json.dumps(msg)+"\\n")
    return msg
def put(message):
    raw=json.dumps(message).encode()
    sys.stdout.buffer.write(struct.pack(">I",len(raw))+raw);sys.stdout.buffer.flush()
def tool(n,op,**args):
    put({"type":"request","id":n,"op":op,**args})
    response=get();assert response["id"]==n
    return response.get("result",False)
assert get()=={"type":"open","version":1}
run.write_text(str(os.getpid()))
current="gemini-test-low";models=[current,"gemini-test-high","gemini-declined"]
put({"type":"ready","model":current,"models":models})
while True:
    req=get()
    if req["type"]=="close":break
    if req["type"]=="set_model":
        if req["model"]=="gemini-declined":put({"type":"model","model":current,"error":"denied"})
        else:
            current=req["model"];put({"type":"model","model":current})
        continue
    command=req["text"]
    if command=="idle":time.sleep(30)
    if command in ("edit","forged"):
        before=tool(1,"read",path="note.txt")
        proposed=before.replace("pending","done")
        if tool(2,"approve",path="note.txt",content=proposed):
            ok=tool(3,"write",path="note.txt",content="FORGED" if command=="forged" else proposed)
            put({"type":"text","text":"written" if ok else "not written"})
    elif command=="parallel":
        for n,p in enumerate(("a.txt","b.txt")):assert tool(n+1,"approve",path=p,content=p)
        for n,p in enumerate(("a.txt","b.txt")):assert tool(n+3,"write",path=p,content=p)
    elif command=="test":
        result=tool(1,"test");put({"type":"text","text":str(result)})
    elif command=="mkdir":
        result=tool(1,"mkdir",path="folder");put({"type":"text","text":str(result)})
    elif command=="probe":
        for n,p in enumerate(("../outside.txt",".ssh/id_ed25519","symlink")):
            assert not tool(n+1,"read",path=p)
        assert "note.txt" in tool(4,"list",path=".")
        assert not tool(5,"write",path="note.txt",content="changed")
    else:put({"type":"text","text":"echo: "+command})
    put({"type":"end","reason":"end_turn"})
'''


class Sink:
    def __init__(self):
        self.inbox = queue.Queue()

    def write(self, raw):
        self.inbox.put(json.loads(raw))

    def flush(self):
        pass


class Harness:
    def __init__(self, root, *, test=None, test_folder=None):
        self.root = root
        self.pid = root / "pid"
        self.log = root / "wire.jsonl"
        self.project = root / "project"
        self.project.mkdir()
        self.note = self.project / "note.txt"
        self.note.write_text("local marker\nstatus=pending\n")
        self.output = Sink()
        self.bridge = acp.Bridge(
            [sys.executable, "-I", "-B", "-c", SLOT, str(self.log), str(self.pid)],
            {"PATH": os.defpath}, editor, workspace_tools, project_files, root / "private",
            test, test_folder, io.BytesIO(), self.output,
        )
        self.history = []
        self.seq = 0

    def send(self, method, params=None, *, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.seq += 1
            msg["id"] = self.seq
        self.bridge.handle(msg)
        return msg.get("id")

    def get(self):
        m = self.output.inbox.get(timeout=6)
        self.history.append(m)
        return m

    def recv(self, ident, answer=None):
        while True:
            msg = self.get()
            if "method" not in msg and msg.get("id") == ident:
                return msg
            if msg.get("method") == "session/request_permission":
                assert answer, "Unexpected approval"
                value = answer(msg)
                self.bridge.handle({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "outcome": {"outcome": "selected", "optionId": value}}})

    def open(self):
        init = self.recv(self.send("initialize", {"protocolVersion": 2}))
        assert init["result"]["protocolVersion"] == 1
        s = self.recv(self.send("session/new", {
            "cwd": str(self.project), "mcpServers": [{"name": "ignored", "command": "/unused"}],
        }))
        return s["result"]["sessionId"]

    def prompt(self, sid, text):
        return self.send("session/prompt", {"sessionId": sid, "prompt": [
            {"type": "text", "text": text}]})

    def close(self):
        self.bridge.serve()  # EOF triggers the production teardown path.


@pytest.fixture
def host(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.close()


def test_discovery_real_framing_models_and_normal_turns(host):
    sid = host.open()
    assert host.recv(host.send("session/new", {"cwd": str(host.project)}))["error"]
    assert host.recv(host.send("session/load", {"sessionId": sid}))["error"]["code"] == -32601
    info = host.recv(host.send("session/set_config_option", {
        "sessionId": sid, "configId": "model", "value": "gemini-test-high"}))
    assert info["result"]["configOptions"][0]["currentValue"] == "gemini-test-high"
    for m in ("not-a-model", "gemini-declined"):
        assert "error" in host.recv(host.send("session/set_model", {"sessionId": sid, "modelId": m}))
    assert host.bridge.session.current == "gemini-test-high"
    assert "error" in host.recv(host.prompt("wrong", "private"))
    for message in ("one", "two"):
        assert host.recv(host.prompt(sid, message))["result"] == {"stopReason": "end_turn"}
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["type"] for m in wire] == ["open", "set_model", "set_model", "prompt", "prompt", "close"]
    assert [m["text"] for m in wire if "text" in m] == ["one", "two"]
    assert str(host.project) not in host.log.read_text() and "mcpServers" not in host.log.read_text()


@pytest.mark.parametrize("choice", ["allow", "decline", "allow_always", "stale", "forged"])
def test_write_requires_local_preview_one_time_consent_and_stable_content(host, choice):
    sid = host.open()
    original = host.note.read_text()

    def approve(msg):
        tool = msg["params"]["toolCall"]
        content, = tool["content"]
        assert content == {"type": "diff", "path": str(host.note), "oldText": original,
                           "newText": original.replace("pending", "done")}
        assert {o["kind"] for o in msg["params"]["options"]} == {"allow_once", "reject_once"}
        # The visible preview precedes T3's permission card.
        text = [m["params"]["update"].get("content", {}) for m in host.history
                if m.get("method") == "session/update"
                and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
        assert any("-status=pending" in c["text"] and "+status=done" in c["text"] for c in text)
        if choice == "stale":
            host.note.write_text("another editor")
        return "allow" if choice in ("stale", "forged") else choice

    assert host.recv(host.prompt(sid, "forged" if choice == "forged" else "edit"), approve)["result"] == {"stopReason": "end_turn"}
    expected = original.replace("pending", "done") if choice == "allow" else original
    assert host.note.read_text() == ("another editor" if choice == "stale" else expected)


def test_parallel_file_grants_and_denied_or_approved_mkdir(host):
    sid = host.open()
    host.recv(host.prompt(sid, "parallel"), lambda _: "allow")
    assert [p.read_text() for p in (host.project / "a.txt", host.project / "b.txt")] == ["a.txt", "b.txt"]
    host.recv(host.prompt(sid, "mkdir"), lambda _: "decline")
    assert not (host.project / "folder").exists()
    host.recv(host.prompt(sid, "mkdir"), lambda _: "allow")
    assert (host.project / "folder").is_dir()


def test_file_confinement_and_disabled_tests(host):
    sid = host.open()
    protected = host.root / "outside.txt"
    protected.write_text("private")
    (host.project / "symlink").symlink_to(protected)
    assert host.recv(host.prompt(sid, "probe"))["result"]["stopReason"] == "end_turn"
    assert host.recv(host.prompt(sid, "test"))["result"]["stopReason"] == "end_turn"
    assert "private" not in host.log.read_text()
    assert host.note.read_text().endswith("pending\n")


def test_text_and_workspace_file_references(host):
    sid = host.open()
    ident = host.send("session/prompt", {"sessionId": sid, "prompt": [
        {"type": "text", "text": "Use this"}, {"type": "resource_link", "uri": host.note.as_uri()}]})
    assert host.recv(ident)["result"]["stopReason"] == "end_turn"
    assert "Use this\\nReferenced project file: note.txt" in host.log.read_text()
    for block in ({"type": "image", "data": "abc"}, {"type": "resource_link", "uri": (host.root / "outside").as_uri()},
                  {"type": "resource_link", "uri": (host.project / ".ssh/key").as_uri()},
                  {"type": "text", "text": "a" * 65537}):
        reply = host.recv(host.send("session/prompt", {"sessionId": sid, "prompt": [block]}))
        assert reply["error"]["code"] == -32602


def test_cancel_and_late_approval_cannot_mutate_file(host):
    sid = host.open()
    original = host.note.read_text()
    turn = host.prompt(sid, "edit")
    while True:
        approval = host.get()
        if approval.get("method") == "session/request_permission":
            break
    assert host.recv(host.prompt(sid, "overlap"))["error"]["code"] == -32603
    pid = int(host.pid.read_text())
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    assert host.recv(turn)["result"]["stopReason"] == "cancelled"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    host.bridge.handle({"jsonrpc": "2.0", "id": approval["id"], "result": {
        "outcome": {"outcome": "selected", "optionId": "allow"}}})
    assert host.note.read_text() == original
    assert "error" in host.recv(host.prompt(sid, "next"))
    new = host.recv(host.send("session/new", {"cwd": str(host.project)}))
    assert new["result"]["sessionId"] != sid


def steering_peer(host, turns):
    setup = SLOT.split('put({"type":"ready"', 1)[0]
    host.bridge.command[4] = setup + """
put({"type":"ready","model":current,"models":models,"capabilities":{"cancelTurn":True}})
""" + turns


@pytest.mark.parametrize("finish", ["cancelled", "end_turn"])
def test_steer_keeps_session_and_rejects_late_old_turn_actions(host, finish):
    steering_peer(host, f"finish = {finish!r}\n" + """
first = get()
assert first == {"type":"prompt","turn":1,"text":"edit this file"}
put({"type":"request","op":"approve","id":1,"path":"draft.txt","content":"old"})
assert get() == {"type":"cancel","turn":1}
assert "error" in get()
put({"type":"text","text":"obsolete output"})
assert not tool(2,"write",path="draft.txt",content="old")
put({"type":"end","reason":finish,"turn":1})
second = get()
assert second == {"type":"prompt","turn":2,"text":"show me the preview"}
assert not tool(3,"write",path="draft.txt",content="old")
put({"type":"text","text":"Preview in the original conversation"})
put({"type":"end","reason":"end_turn","turn":2})
assert get() == {"type":"close"}
""")
    sid = host.open()
    process = host.bridge.session.process
    first = host.prompt(sid, "edit this file")
    while True:
        msg = host.get()
        if msg.get("method") == "session/request_permission":
            break
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    assert host.recv(first)["result"] == {"stopReason": "cancelled"}
    host.bridge.handle({"jsonrpc": "2.0", "id": msg["id"], "result": {
        "outcome": {"outcome": "selected", "optionId": "allow"}}})
    assert host.recv(host.prompt(sid, "show me the preview"))["result"] == {"stopReason": "end_turn"}
    assert host.bridge.session.process is process and process.poll() is None
    assert not (host.project / "draft.txt").exists()
    texts = [m["params"]["update"]["content"]["text"] for m in host.history
             if m.get("method") == "session/update"
             and m["params"]["update"]["sessionUpdate"] == "agent_message_chunk"]
    assert "Preview in the original conversation" in texts
    assert not any("obsolete" in text or "new T3 thread" in text for text in texts)
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}


@pytest.mark.parametrize("end", ["unresponsive", "wrong-turn", "closed"])
def test_unconfirmed_cancel_discards_only_the_broken_session(host, monkeypatch, end):
    monkeypatch.setattr(acp, "CANCEL_GRACE", 0.15)
    steering_peer(host, f"end = {end!r}\n" + """
req = get()
put({"type":"text","text":"started"})
assert get() == {"type":"cancel","turn":1}
if end == "unresponsive":
    time.sleep(30)
elif end == "wrong-turn":
    put({"type":"end","reason":"cancelled","turn":2})
    time.sleep(30)
""")
    sid = host.open()
    first = host.prompt(sid, "start")
    while host.get().get("method") != "session/update":
        pass
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    assert host.recv(first)["result"] == {"stopReason": "cancelled"}
    assert host.bridge.session is None
    assert "error" in host.recv(host.prompt(sid, "follow-up"))
    frames = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [f["text"] for f in frames if f["type"] == "prompt"] == ["start"]


def test_steer_before_a_prompt_is_sent_does_not_send_or_replay_it(host, monkeypatch):
    steering_peer(host, """
req = get()
assert req == {"type":"prompt","turn":1,"text":"show me the preview"}
put({"type":"end","reason":"end_turn","turn":1})
assert get() == {"type":"close"}
""")
    sid = host.open()
    entered, proceed = threading.Event(), threading.Event()
    normalize = acp.prompt_text

    def hold(*args):
        entered.set()
        assert proceed.wait(3)
        return normalize(*args)

    monkeypatch.setattr(acp, "prompt_text", hold)
    first = host.prompt(sid, "do not forward")
    assert entered.wait(3)
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    proceed.set()
    assert host.recv(first)["result"] == {"stopReason": "cancelled"}
    assert host.recv(host.prompt(sid, "show me the preview"))["result"] == {"stopReason": "end_turn"}
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}
    assert "do not forward" not in host.log.read_text()


def test_steer_interrupts_local_test_before_followup(tmp_path, monkeypatch):
    host = Harness(tmp_path, test="pytest")
    steering_peer(host, """
assert get()["turn"] == 1
put({"type":"request","op":"test","id":1})
assert get() == {"type":"cancel","turn":1}
assert "error" in get()
put({"type":"end","reason":"cancelled","turn":1})
assert get()["turn"] == 2
put({"type":"end","reason":"end_turn","turn":2})
assert get() == {"type":"close"}
""")
    started = threading.Event()
    child = []
    spawn = subprocess.Popen

    def run_client_test(command, **kwargs):
        if command[0] != "/usr/bin/sandbox-exec":
            return spawn(command, **kwargs)
        proc = spawn([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        child.append(proc)
        started.set()
        return proc

    monkeypatch.setattr(subprocess, "Popen", run_client_test)
    original_which = workspace_tools.shutil.which
    monkeypatch.setattr(workspace_tools.shutil, "which", lambda cmd, **kwargs:
                        sys.executable if cmd == "pytest" else original_which(cmd, **kwargs))
    try:
        sid = host.open()
        first = host.prompt(sid, "test")
        while True:
            msg = host.get()
            if msg.get("method") == "session/request_permission":
                break
        host.bridge.handle({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "outcome": {"outcome": "selected", "optionId": "allow"}}})
        assert started.wait(3)
        host.send("session/cancel", {"sessionId": sid}, notify=True)
        assert host.recv(first)["result"] == {"stopReason": "cancelled"}
        assert child[0].poll() == -signal.SIGKILL
        assert host.recv(host.prompt(sid, "follow-up"))["result"] == {"stopReason": "end_turn"}
    finally:
        host.close()


def test_session_close_during_steer_releases_slot_without_waiting_for_ack(host):
    steering_peer(host, """
assert get()["turn"] == 1
put({"type":"text","text":"running"})
assert get() == {"type":"cancel","turn":1}
time.sleep(30)
""")
    sid = host.open()
    proc = host.bridge.session.process
    first = host.prompt(sid, "start")
    while host.get().get("method") != "session/update":
        pass
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}
    assert host.recv(first)["result"] == {"stopReason": "cancelled"}
    assert proc.poll() is not None and host.bridge.session is None


def test_parse_errors_and_shutdown_without_initialize():
    sink = Sink()
    bridge = acp.Bridge([], {}, editor, workspace_tools, project_files, "/", None, None,
                        io.BytesIO(b"invalid\n{}\n"), sink)
    assert bridge.serve() == 0
    assert sink.inbox.get()["error"]["code"] == -32700
    assert sink.inbox.get()["error"]["code"] == -32600
    with pytest.raises(acp.RpcError):
        acp.prompt_text([], 100, None)


def test_workspace_test_cancellation_reaps_runner_with_closed_stdout(tmp_path, monkeypatch):
    # Simulate a user test that closed its pipes then kept running; no PSI/OS-specific sandbox required.
    cancelled = threading.Event()
    directory = tmp_path / "project"
    directory.mkdir()
    runner = workspace_tools.Workspace(directory, lambda *_: True, "pytest", cancelled=cancelled.is_set)
    real_popen = subprocess.Popen
    child = []

    def spawn(cmd, **opts):
        del cmd
        proc = real_popen([sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(20)"], **opts)
        child.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", spawn)
    timer = threading.Timer(0.4, cancelled.set)
    timer.start()
    try:
        with pytest.raises(ValueError, match="cancelled"):
            runner.call("test_project", {})
        assert child and child[0].poll() == -signal.SIGKILL
    finally:
        cancelled.set()
        timer.cancel()
        runner.close()


def test_help_and_model_text_commands_stay_out_of_generation(host):
    sid = host.open()
    for text in ("/help", "/model", "/model 2", "/model invalid-model"):
        assert host.recv(host.prompt(sid, text))["result"]["stopReason"] == "end_turn"
    assert host.bridge.session.current == "gemini-test-high"
    frames = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert frames == [{"type": "open", "version": 1}, {"type": "set_model", "model": "gemini-test-high"}]
    updates = [m["params"]["update"] for m in host.history if m.get("method") == "session/update"]
    assert any("Supervised" in u.get("content", {}).get("text", "") for u in updates)
    assert any("Current model: gemini-test-low" in u.get("content", {}).get("text", "") for u in updates)
    assert any(u.get("sessionUpdate") == "config_option_update" for u in updates)


def test_dead_transport_while_waiting_for_approval(host):
    sid = host.open()
    before = host.note.read_bytes()
    rid = host.prompt(sid, "edit")
    while host.get().get("method") != "session/request_permission":
        pass
    os.kill(int(host.pid.read_text()), signal.SIGKILL)
    assert "error" in host.recv(rid)
    assert host.note.read_bytes() == before
    assert host.bridge.session.id == sid and host.bridge.session.process is None
    assert not host.bridge.pending and not host.bridge.session.approved


def test_sigterm_reaps_the_separate_ssh_process_group(tmp_path):
    import threading
    from pathlib import Path

    repo = Path(acp.__file__).resolve().parents[1]
    launcher = """import sys,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from ccfleet_agent import gemini_acp_client as acp, gemini_editor_client as ed, workspace_tools as tools, project_files
acp.run([sys.executable,'-I','-c',sys.argv[3],sys.argv[2]+'/wire',sys.argv[2]+'/pid'],{'PATH':os.defpath},ed,tools,project_files,fleet_home=Path(sys.argv[2])/'private')
"""
    proc = subprocess.Popen([sys.executable, "-I", "-c", launcher, str(repo), str(tmp_path), SLOT],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    inbox = queue.Queue()

    def read():
        for line in proc.stdout:
            inbox.put(json.loads(line))

    threading.Thread(target=read, daemon=True).start()

    def call(i, name, params):
        proc.stdin.write(json.dumps({"jsonrpc":"2.0", "id":i, "method":name, "params":params}) + "\n")
        proc.stdin.flush()
        return inbox.get(timeout=5)

    try:
        assert call(1, "initialize", {"protocolVersion":1})["id"] == 1
        assert call(2, "session/new", {"cwd": str(tmp_path), "mcpServers":[]})["id"] == 2
        child = int((tmp_path / "pid").read_text())
        proc.terminate()
        assert proc.wait(timeout=8) == 128 + signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            os.kill(child,0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdin,proc.stdout,proc.stderr):
            stream.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="uses the Mac local project test sandbox")
def test_local_project_runner_requires_one_time_permission(tmp_path, monkeypatch):
    import shutil

    trusted_path = "/opt/homebrew/bin:/usr/bin:/bin"
    if not shutil.which("npm", path=trusted_path):
        pytest.skip("npm is unavailable")
    monkeypatch.setenv("PATH", trusted_path)
    h = Harness(tmp_path, test="npm")
    (h.project / "package.json").write_text(json.dumps({"scripts": {"test": "node check.cjs"}}))
    (h.project / "check.cjs").write_text("require('node:fs').writeFileSync('checked.txt','ok'); console.log('RESULT-FROM-PROJECT')")
    try:
        sid = h.open()
        assert h.recv(h.prompt(sid, "test"), lambda _: "decline")["result"]["stopReason"] == "end_turn"
        assert not (h.project / "checked.txt").exists()

        def approve(msg):
            tc = msg["params"]["toolCall"]
            assert tc["kind"] == "execute" and tc["title"].endswith("npm test")
            assert tc["locations"] == [{"path": str(h.project)}]
            return "allow"
        assert h.recv(h.prompt(sid, "test"), approve)["result"]["stopReason"] == "end_turn"
        assert (h.project / "checked.txt").read_text() == "ok"
        wire = [json.loads(line) for line in h.log.read_text().splitlines()]
        result = [v["result"] for v in wire if isinstance(v.get("result"), dict)]
        assert result[-1]["exit"] == 0 and "RESULT-FROM-PROJECT" in result[-1]["output"]
    finally:
        h.close()


def admission_slot(host, errors):
    """Record child ownership; leave a refused peer running until its own SSH is reaped."""
    attempts = host.root / "attempts"
    inserted = """
attempts=log.parent/'attempts'
with attempts.open('a') as f:f.write(str(os.getpid())+'\\n')
index=len(attempts.read_text().splitlines())-1
errors=json.loads(sys.argv[3])
if index<len(errors):
    put({'type':'error','message':errors[index]})
    time.sleep(20)
    sys.exit(2)
"""
    host.bridge.command[4] = SLOT.replace('run.write_text(str(os.getpid()))', inserted)
    host.bridge.command.append(json.dumps(errors))
    return attempts


def test_busy_discovery_retries_only_before_open_and_default_is_not_a_model(host, monkeypatch):
    monkeypatch.setattr(acp, "BUSY_RETRY_DELAY", .01)
    attempts = admission_slot(host, [acp.SLOT_BUSY])
    sid = host.open()
    pids = list(map(int, attempts.read_text().splitlines()))
    assert len(pids) == 2
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    # Both supported ACP model methods can receive T3's default placeholder.
    assert host.recv(host.send("session/set_config_option", {
        "sessionId": sid, "configId": "model", "value": "default",
    }))["result"]["configOptions"][0]["currentValue"] == "gemini-test-low"
    assert host.recv(host.send("session/set_model", {"sessionId": sid, "modelId": "default"}))["result"] == {}
    assert "default" not in host.bridge.session.models
    assert host.recv(host.prompt(sid, "sent once"))["result"]["stopReason"] == "end_turn"
    assert [json.loads(s)["type"] for s in host.log.read_text().splitlines()] == ["open", "open", "prompt"]


@pytest.mark.parametrize("message,is_busy", [
    (acp.SLOT_BUSY, True), ("Grant revoked", False),
    ("Another Gemini editor is using this slot, but unrecognized error", False),
    ("Sign in to the Antigravity editor", False),
])
def test_busy_exhaustion_and_nonretryable_open_errors(host, monkeypatch, message, is_busy):
    monkeypatch.setattr(acp, "BUSY_RETRY_WINDOW", .14)
    monkeypatch.setattr(acp, "BUSY_RETRY_DELAY", .01)
    attempts = admission_slot(host, [message] * 32)
    host.recv(host.send("initialize", {"protocolVersion": 1}))
    reply = host.recv(host.send("session/new", {"cwd": str(host.project)}))
    assert ("using your slot" in reply["error"]["message"]) == is_busy
    assert_t3_auth_state(reply["error"], message == "Sign in to the Antigravity editor")
    pids = list(map(int, attempts.read_text().splitlines()))
    assert (1 < len(pids) < 12) if is_busy else len(pids) == 1
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert host.bridge.session is None


def test_disconnecting_editor_ends_busy_backoff(host, monkeypatch):
    import time

    attempts = admission_slot(host, [acp.SLOT_BUSY] * 8)
    monkeypatch.setattr(acp, "BUSY_RETRY_DELAY", 3)
    host.recv(host.send("initialize", {"protocolVersion": 1}))
    host.send("session/new", {"cwd": str(host.project)})
    until = time.monotonic() + 4
    while not attempts.exists() and time.monotonic() < until:
        time.sleep(.01)
    assert attempts.exists()
    host.close()
    assert len(attempts.read_text().splitlines()) == 1
    assert not host.bridge.worker.is_alive()


def assert_t3_auth_state(error, requires_auth):
    # T3 classifies either the reserved ACP code or sign-in wording as an auth failure.
    classified = error["code"] == -32000 or re.search(
        r"auth|credential|log.?in|sign.?in", error["message"], re.IGNORECASE) is not None
    assert classified is requires_auth
    assert error["code"] == (-32000 if requires_auth else -32603)


def open_reply(host):
    host.recv(host.send("initialize", {"protocolVersion": 1}))
    return host.recv(host.send("session/new", {"cwd": str(host.project)}))


@pytest.mark.parametrize(("stderr", "expected"), [
    (b"ccfleet: broker refused the WebSocket connection (HTTP 401)\n", "ccfleet pair gemini --replace"),
    (b"ccfleet: broker refused the WebSocket connection (HTTP 403)\n", "refused slot access"),
    (b"ccfleet: broker refused the WebSocket connection (HTTP 502)\n", "HTTP 502"),
    (b"ccfleet: broker refused the WebSocket connection (HTTP 503)\n", "HTTP 503"),
    (b"ccfleet: broker refused the WebSocket connection (HTTP 504)\n", "HTTP 504"),
    (b"", acp.OPEN_TRANSPORT),
    (b"private-ssh-detail\n", acp.OPEN_TRANSPORT),
    (b"x" * 1024 + b"ccfleet: broker refused the WebSocket connection (HTTP 401)\n",
     acp.OPEN_TRANSPORT),
    (b"ccfleet: broker refused the WebSocket connection (HTTP 401) private-ssh-detail\n",
     acp.OPEN_TRANSPORT),
])
def test_open_reports_only_complete_allowlisted_transport_status(host, stderr, expected):
    # Produce more than a pipeful before the connection closes and use the actual Channel reader.
    program = ("import os, sys; os.write(2, b'x'*100000 + b'\\n' + bytes.fromhex(sys.argv[1])); "
               "sys.stdin.buffer.read(4)")
    host.bridge.command = [sys.executable, "-I", "-c", program, stderr.hex()]
    reply = open_reply(host)
    assert_t3_auth_state(reply["error"], stderr == b"ccfleet: broker refused the WebSocket connection (HTTP 401)\n")
    message = reply["error"]["message"]
    assert expected in message and "private-ssh-detail" not in message
    if b"403" in stderr:
        assert "--replace" not in message
    assert host.bridge.session is None


@pytest.mark.parametrize(("reason", "expected"), [
    ("The Gemini editor is unavailable on the private Mac. Reopen it to try again.",
     "private Mac could not start its Gemini editor"),
    ("The Gemini slot timed out. Reopen the editor to continue.", acp.OPEN_TIMEOUT),
    ("The Google editor agent stopped on the slot.", "Antigravity stopped during startup"),
    ("The signed-in Google account has no available Gemini editor model.", "no Gemini models"),
    ("The Google editor declined the model change.", "declined the starting Gemini model"),
    ("Sign in to the Antigravity editor", "Open CCFleet Gemini Sign In"),
    ("Open CCFleet Gemini Sign In in the private Mac owner's Applications folder, then reconnect.",
     "Open CCFleet Gemini Sign In"),
    ("Unknown /private/provider/path and private-key data", acp.OPEN_REFUSED),
    ("Sign in to the Antigravity editor: private-key", acp.OPEN_REFUSED),
])
def test_open_slot_diagnostics_stay_pre_prompt_private_and_nonretryable(host, reason, expected):
    attempts = admission_slot(host, [reason])
    reply = open_reply(host)
    assert_t3_auth_state(reply["error"], reason in acp.SLOT_LOGIN_REQUIRED)
    assert expected in reply["error"]["message"]
    assert "private-key" not in reply["error"]["message"]
    assert len(attempts.read_text().splitlines()) == 1
    assert host.bridge.session is None


@pytest.mark.parametrize(("response", "expected"), [
    ({"type": "ready", "model": "gemini-test", "models": ["bad/private"]}, acp.BAD_READY),
    ({"type": "text", "text": "private-documents"}, acp.BAD_READY),
])
def test_open_rejects_invalid_startup_reply_without_sending_a_prompt(host, response, expected):
    start = SLOT.index('put({"type":"ready"')
    host.bridge.command[4] = SLOT[:start] + "put(" + repr(response) + ");sys.exit(0)"
    assert open_reply(host)["error"] == {"code": -32603, "message": expected}
    assert [json.loads(line)["type"] for line in host.log.read_text().splitlines()] == ["open"]


def test_open_local_transport_and_startup_timeout(host, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("private path")

    monkeypatch.setattr(editor.Channel, "receive", timeout)
    assert open_reply(host)["error"]["message"] == acp.OPEN_TIMEOUT
    host.bridge.command = [str(host.root / "absent command")]
    result = host.recv(host.send("session/new", {"cwd": str(host.project)}))
    assert result["error"]["message"] == "CCFleet could not start the local SSH transport."


def test_reported_session_expiry_survives_a_closed_ssh_write_pipe(host):
    # The server expires an established session between requests. The final diagnostic
    # can already be queued on stdout when writing the next request gets a broken pipe.
    replacement = f"put({{'type': 'error', 'message': {acp.SLOT_SESSION_TIMEOUT!r}}});sys.exit(2)"
    host.bridge.command[4] = SLOT.partition("while True:\n    req=get()")[0] + replacement
    sid = host.open()
    host.bridge.session.process.wait(timeout=5)
    res = host.recv(host.prompt(sid, "must not be retried"))
    assert "slot reported that this Gemini session timed out" in res["error"]["message"]
    assert host.bridge.session.id == sid and host.bridge.session.process is None
    assert [json.loads(line)["type"] for line in host.log.read_text().splitlines()] == ["open"]


def reconnecting_peer(host, *attachments):
    # A replacement process represents a new authenticated SSH attachment to the same slot.
    framing = SLOT.split('assert get()=={"type":"open"', 1)[0]
    setup = """
index_path = log.with_name("attachments")
index = int(index_path.read_text()) if index_path.exists() else 0
index_path.write_text(str(index + 1))
token = "ab" * 32   # Fixed, synthetic resume capability, deliberately absent from ACP output.
def ready(resumed=False, expected=None, *, enable=True):
    opener = get()
    assert opener == ({"type":"resume", "version":1, "resumeToken":token} if resumed
                      else {"type":"open","version":1})
    put({"type":"ready","model":"gemini-low","models":["gemini-low","gemini-high"],
         "capabilities":{"cancelTurn":True,"resumeSession":True},
         "resumeToken":token if expected is None else expected, "resumed":resumed})
    if enable:
        assert get()=={"type":"enableResume"}
"""
    host.bridge.command[4] = framing + setup + "\nexec(" + repr(attachments) + "[index])"


def test_reconnect_restores_original_context_and_model_without_replaying_the_turn(host):
    reconnecting_peer(host, """
ready(enable=False)
assert get()=={"type":"set_model","model":"gemini-high"}
put({"type":"model","model":"gemini-high"})
assert get()=={"type":"enableResume"}
first=get()
put({"type":"progress","turn":first["turn"]})
put({"type":"text","text":"partial output"})
""", """
ready(resumed=True)
assert get()=={"type":"set_model","model":"gemini-high"}
put({"type":"model","model":"gemini-high"})
turn=get()
assert turn["text"]=="show me the result"
put({"type":"end","turn":turn["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    session = host.bridge.session
    host.recv(host.send("session/set_model", {"sessionId": sid, "modelId": "gemini-high"}))
    first = session.process
    interrupted = host.recv(host.prompt(sid, "perform side effects once"))
    assert "will not resend" in interrupted["error"]["message"]
    assert_t3_auth_state(interrupted["error"], False)
    assert first.poll() is not None and session.process is None
    assert host.bridge.session is session
    assert host.recv(host.prompt(sid, "show me the result"))["result"] == {"stopReason": "end_turn"}
    assert session.current == "gemini-high" and session.id == sid
    host.recv(host.send("session/close", {"sessionId": sid}))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["text"] for m in wire if m["type"] == "prompt"] == [
        "perform side effects once", "show me the result",
    ]
    assert [m["type"] for m in wire].count("open") == 1
    assert [m["type"] for m in wire].count("resume") == 1
    assert "ab" * 32 not in json.dumps(host.history)


def test_missing_native_resume_needs_explicit_fresh_context(host):
    reconnecting_peer(host, "ready(); get()", """
assert get()=={"type":"resume","version":1,"resumeToken":token}
put({"type":"error","message":"The previous Gemini session cannot be resumed."})
""", """
ready()
p=get();assert p["text"]=="new instructions"
put({"type":"end","turn":p["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    assert "error" in host.recv(host.prompt(sid, "cut wire"))
    res = host.recv(host.prompt(sid, "must not create a new thread"))
    assert "/reconnect" in res["error"]["message"]
    assert "error" in host.recv(host.prompt(sid, "also not forwarded"))
    assert host.recv(host.prompt(sid, "/reconnect"))["result"]["stopReason"] == "end_turn"
    assert any("fresh Gemini conversation" in str(m) for m in host.history)
    assert host.recv(host.prompt(sid, "new instructions"))["result"]["stopReason"] == "end_turn"
    host.recv(host.send("session/close", {"sessionId": sid}))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["text"] for m in wire if m["type"] == "prompt"] == ["cut wire", "new instructions"]


def test_temporary_reconnect_failure_keeps_token_and_revoked_device_cannot_continue(host):
    reconnecting_peer(host, "ready(); get()", """
assert get()["type"]=="resume"
print("ccfleet: broker refused the WebSocket connection (HTTP 503)",file=sys.stderr)
""", """
assert get()["type"]=="resume"
print("ccfleet: broker refused the WebSocket connection (HTTP 401)",file=sys.stderr)
""")
    sid = host.open()
    assert "error" in host.recv(host.prompt(sid, "cut wire"))
    res = host.recv(host.prompt(sid, "new one"))
    assert "503" in res["error"]["message"] and host.bridge.session.process is None
    assert_t3_auth_state(res["error"], False)
    res = host.recv(host.prompt(sid, "new two"))
    assert "401" in res["error"]["message"] and host.bridge.session is None
    assert_t3_auth_state(res["error"], True)
    assert "error" in host.recv(host.prompt(sid, "/reconnect"))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["type"] for m in wire] == ["open", "enableResume", "prompt", "resume", "resume"]


@pytest.mark.parametrize("incorrect", [
    'put({"type":"ready","model":"gemini-low"})',
    'put({"type":"ready","model":"gemini-low","resumed":False,"resumeToken":token,'
    '"capabilities":{"resumeSession":True}})',
    'put({"type":"ready","model":"gemini-low","resumed":True,"resumeToken":"cd"*32,'
    '"capabilities":{"resumeSession":True}})',
])
def test_resume_requires_confirmation_from_the_same_slot_context(host, incorrect):
    reconnecting_peer(host, "ready(); get()", "assert get()['type']=='resume'\n" + incorrect)
    sid = host.open()
    host.recv(host.prompt(sid, "old"))
    result = host.recv(host.prompt(sid, "must not be sent on wrong context"))
    assert result["error"]["message"] == acp.BAD_READY
    assert host.bridge.session is None
    assert [json.loads(s)["type"] for s in host.log.read_text().splitlines()].count("prompt") == 1


def test_disconnected_legacy_slot_can_start_a_fresh_context_only_on_request(host):
    host.bridge.command[4] = SLOT.replace('command=req["text"]',
                                         'command=req["text"]\n    if command=="cut":sys.exit()')
    sid = host.open()
    result = host.recv(host.prompt(sid, "cut"))
    assert "/reconnect" in result["error"]["message"]
    assert "error" in host.recv(host.prompt(sid, "/reconnect replay something"))
    assert "error" in host.recv(host.prompt(sid, "don't retry automatically"))
    assert host.recv(host.prompt(sid, "/reconnect"))["result"]["stopReason"] == "end_turn"
    assert host.recv(host.prompt(sid, "continue fresh"))["result"]["stopReason"] == "end_turn"
    frames = [json.loads(s) for s in host.log.read_text().splitlines()]
    assert [m["text"] for m in frames if m["type"] == "prompt"] == ["cut", "continue fresh"]


def test_idle_resume_sends_new_instruction_once_and_close_invalidates_it(host):
    reconnecting_peer(host, """
ready()
prior=get();put({"type":"end","turn":prior["turn"],"reason":"end_turn"})
""", """
ready(resumed=True)
turn=get();assert turn["text"]=="first message after idle"
put({"type":"end","turn":turn["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    first_process = host.bridge.session.process
    assert host.recv(host.prompt(sid, "earlier turn"))["result"]["stopReason"] == "end_turn"
    first_process.wait(timeout=5)
    assert host.recv(host.prompt(sid, "first message after idle"))["result"]["stopReason"] == "end_turn"
    host.recv(host.send("session/close", {"sessionId": sid}))
    assert "error" in host.recv(host.prompt(sid, "late message"))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["text"] for m in wire if m["type"] == "prompt"] == [
        "earlier turn", "first message after idle",
    ]


def test_active_close_relays_explicit_close_and_removes_local_resume_capability(host):
    reconnecting_peer(host, """
ready();get()
put({"type":"text","text":"waiting"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    session = host.bridge.session
    turn = host.prompt(sid, "active")
    while host.get().get("method") != "session/update":
        pass
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}
    assert host.recv(turn)["result"]["stopReason"] == "cancelled"
    assert host.bridge.session is None and session.resume_token is None
    assert "error" in host.recv(host.prompt(sid, "late message"))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["type"] for m in wire] == ["open", "enableResume", "prompt", "close"]


@pytest.mark.parametrize("frame", [
    {"type": "progress", "turn": 99},
    {"type": "progress", "turn": True},
    {"type": "progress", "turn": 1, "content": "private"},
])
def test_invalid_progress_frame_invalidates_context(host, frame):
    reconnecting_peer(host, 'ready(); get(); put(' + repr(frame) + ')')
    sid = host.open()
    result = host.recv(host.prompt(sid, "one"))
    assert "error" in result and "private" not in repr(result.get("error", {}).get("data", ""))
    assert host.bridge.session is None and "error" in host.recv(host.prompt(sid, "two"))


def test_unanswered_approval_cannot_cross_a_resume_boundary(host):
    reconnecting_peer(host, """
ready()
first=get()
assert tool(1,"read",path="note.txt")
put({"type":"request","op":"approve","id":2,"path":"note.txt","content":"unapproved"})
time.sleep(30)
""", """
ready(resumed=True)
turn=get()
assert not tool(3,"write",path="note.txt",content="unapproved")
assert not tool(4,"approve",path="note.txt",content="new attempt without reading")
put({"type":"end","turn":turn["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    first = host.prompt(sid, "propose an edit")
    before = host.note.read_bytes()
    while True:
        approval = host.get()
        if approval.get("method") == "session/request_permission":
            break
    os.kill(host.bridge.session.process.pid, signal.SIGKILL)
    assert "will not resend" in host.recv(first)["error"]["message"]
    assert not host.bridge.pending and not host.bridge.session.seen
    host.bridge.handle({"jsonrpc":"2.0", "id":approval["id"], "result":{
        "outcome":{"outcome":"selected","optionId":"allow"},
    }})
    assert host.recv(host.prompt(sid, "review only"))["result"]["stopReason"] == "end_turn"
    assert host.note.read_bytes() == before


def test_close_of_detached_session_never_reopens_original_context(host):
    reconnecting_peer(host, "ready(); get()")
    sid = host.open()
    original = host.bridge.session
    host.recv(host.prompt(sid, "drop"))
    assert original.resume_token is not None and original.process is None
    assert host.recv(host.send("session/close", {"sessionId": sid}))["result"] == {}
    assert original.resume_token is None and host.bridge.session is None
    assert host.recv(host.prompt(sid, "try again"))["error"]["code"] == -32602
    assert not any(json.loads(line)["type"] == "resume" for line in host.log.read_text().splitlines())


def test_discovery_and_local_commands_never_opt_in_to_retaining_a_profile(host):
    reconnecting_peer(host, """
ready(enable=False)
assert get()=={"type":"close"}
""")
    sid = host.open()
    for prompt in ("/help", "/model", "/reconnect"):
        assert host.recv(host.prompt(sid, prompt))["result"]["stopReason"] == "end_turn"
    assert host.bridge.session.resume_token is None
    host.recv(host.send("session/close", {"sessionId": sid}))
    assert [json.loads(line)["type"] for line in host.log.read_text().splitlines()] == ["open", "close"]


def test_steer_after_transport_retirement_preserves_interruption_notice_and_session(host, monkeypatch):
    reconnecting_peer(host, "ready(); get()", """
ready(resumed=True)
followup=get(); assert followup["text"]=="show the outcome"
put({"type":"end","turn":followup["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    session = host.bridge.session
    retired, settle = threading.Event(), threading.Event()
    detach = session.detach

    def pause_after_retirement():
        detach()
        retired.set()
        assert settle.wait(3)

    monkeypatch.setattr(session, "detach", pause_after_retirement)
    first = host.prompt(sid, "create only once")
    try:
        assert retired.wait(3)
        assert session.channel is None and session.active_turn is not None
        host.send("session/cancel", {"sessionId": sid}, notify=True)
    finally:
        settle.set()
    assert host.recv(first)["result"] == {"stopReason": "cancelled"}
    assert any(acp.TURN_LOST in str(msg) for msg in host.history)
    assert host.bridge.session is session
    assert host.recv(host.prompt(sid, "show the outcome"))["result"] == {"stopReason": "end_turn"}
    host.recv(host.send("session/close", {"sessionId": sid}))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["text"] for m in wire if m["type"] == "prompt"] == [
        "create only once", "show the outcome",
    ]
    assert not any(m["type"] == "cancel" for m in wire)


def test_steer_while_native_resume_is_opening_cancels_only_the_unsent_instruction(host):
    awaiting = host.root / "resume-is-opening"
    reconnecting_peer(host, "ready(); get()", f"""
assert get()=={{"type":"resume","version":1,"resumeToken":token}}
Path({str(awaiting)!r}).touch()
time.sleep(60)
""", """
ready(resumed=True)
turn=get(); assert turn["text"]=="revised instruction"
put({"type":"end","turn":turn["turn"],"reason":"end_turn"})
assert get()=={"type":"close"}
""")
    sid = host.open()
    session = host.bridge.session
    assert "error" in host.recv(host.prompt(sid, "interrupted original"))
    in_flight = host.prompt(sid, "do not send this instruction")
    for _ in range(200):
        if awaiting.exists():
            break
        threading.Event().wait(.01)
    assert awaiting.exists()
    host.send("session/cancel", {"sessionId": sid}, notify=True)
    assert host.recv(in_flight)["result"] == {"stopReason": "cancelled"}
    assert host.bridge.session is session and session.process is None
    assert host.recv(host.prompt(sid, "revised instruction"))["result"] == {"stopReason": "end_turn"}
    host.recv(host.send("session/close", {"sessionId": sid}))
    wire = [json.loads(line) for line in host.log.read_text().splitlines()]
    assert [m["text"] for m in wire if m["type"] == "prompt"] == [
        "interrupted original", "revised instruction",
    ]
    assert [m["type"] for m in wire].count("resume") == 2
