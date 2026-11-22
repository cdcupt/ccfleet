"""The T3 stdio provider retains the workspace and approvals on the connecting Mac."""

import io
import json
import os
import queue
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
    assert host.recv(host.prompt(sid, "overlap"))["error"]["code"] == -32000
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
    assert host.bridge.session is None


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
