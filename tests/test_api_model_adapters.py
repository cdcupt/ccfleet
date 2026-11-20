"""Exercise the Messages adapter against both providers' HTTP formats without paid credentials."""
import asyncio
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from ccfleet_agent import api_model_relay as relay
from ccfleet_agent import inference_client as wire
from ccfleetd.gateway_transport import response_metadata

TOOL = {"name": "Write", "description": "Write a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["file_path", "content"]}}


def upstream_reply(provider, stream, action, file_path):
    args = {"file_path": file_path, "content": "Written through Fleet.\n"}
    if provider == "gemini":
        part = ({"functionCall": {"name": "Write", "args": args}} if action == "write"
                else {"text": "File confirmed."})
        message = {"responseId": "mock1", "modelVersion": "gemini-2.5-flash",
                   "candidates": [{"content": {"role": "model", "parts": [part]},
                                   "finishReason": "STOP", "index": 0}],
                   "usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 8,
                                     "totalTokenCount": 38}}
        return "data: " + json.dumps(message) + "\n\n" if stream else json.dumps(message)
    if action == "write":
        item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Write",
                "arguments": json.dumps(args), "status": "completed"}
        delta = {"type": "response.function_call_arguments.delta", "delta": json.dumps(args)}
    else:
        item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "File confirmed.", "annotations": []}]}
        delta = {"type": "response.output_text.delta", "content_index": 0,
                 "delta": "File confirmed."}
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-5.4",
                "status": "completed", "parallel_tool_calls": True, "tool_choice": "auto",
                "tools": [], "output": [item], "usage": {"input_tokens": 30, "output_tokens": 8,
                                                         "total_tokens": 38}}
    if not stream:
        return json.dumps(response)
    events = [{"type": "response.created", "response": {**response, "output": [],
                                                        "status": "in_progress"}},
              {"type": "response.output_item.added", "output_index": 0,
               "item": {**item, "arguments": ""} if action == "write" else {**item, "content": []}},
              {**delta, "item_id": item["id"], "output_index": 0},
              {"type": "response.output_item.done", "item": item, "output_index": 0},
              {"type": "response.completed", "response": response}]
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps({**e, "sequence_number": i})
                   + "\n\n" for i, e in enumerate(events))


@pytest.fixture
def upstream(tmp_path):
    requests = []
    path = str(tmp_path / "example.txt")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            provider = "openai" if self.path == "/v1/responses" else "gemini"
            assert provider == "openai" or ":generateContent" in self.path or ":streamGenerateContent" in self.path
            requests.append((provider, body))
            content = body.get("input", body.get("contents", []))
            serialized = json.dumps(content)
            action = "confirm" if any(name in serialized for name in (
                "function_call_output", "functionResponse", "function_response")) else "write"
            stream = body.get("stream", False) or ":streamGenerateContent" in self.path
            raw = upstream_reply(provider, stream, action, path).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield SimpleNamespace(url="http://127.0.0.1:" + str(server.server_port) + "/v1",
                          requests=requests, path=path)
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.fixture
def adapter(upstream, monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    sdk = pytest.importorskip("litellm")
    async def create(**kwargs):
        return await sdk.anthropic.messages.acreate(api_base=upstream.url, **kwargs)
    return create


def events(stream):
    data = b"".join(iter(lambda: wire.chunk(stream), b""))
    return [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: ")]


@pytest.mark.parametrize("provider,model", [("openai", "gpt-5.4"), ("gemini", "gemini-2.5-flash")])
def test_real_adapter_translates_tools_and_results(provider, model, upstream, adapter):
    history = [{"role": "user", "content": "Write the requested file"},
               {"role": "system", "content": [{"type": "text", "text": "Keep the edit local."}]}]
    profile = {"provider": provider, "model": model}
    request = {"model": relay.ALIASES[provider], "messages": history,
               "system": [{"type": "text", "text": "System instructions",
                           "cache_control": {"type": "ephemeral"}}],
               "max_tokens": 512, "tools": [TOOL], "stream": True, "metadata": {"user_id": "PRIVATE"},
               "thinking": {"type": "adaptive", "display": "omitted"}, "output_config": {"effort": "high"},
               "context_management": relay.KEEP_THINKING}
    def run():
        opts = relay.model_request(json.dumps(request).encode(), profile)
        output = io.BytesIO()
        asyncio.run(relay.respond(relay.wire._GatewayOutput(output), opts, "fixture-secret", adapter))
        output.seek(0)
        assert response_metadata(output)["status"] == 200
        return output
    first = events(run())
    assert first[-1]["type"] == "message_stop"
    tool = next(e["content_block"] for e in first if e.get("content_block", {}).get("type") == "tool_use")
    arguments = "".join(e["delta"]["partial_json"] for e in first if "partial_json" in e.get("delta", {}))
    assert tool["name"] == "Write" and json.loads(arguments)["file_path"] == upstream.path
    history.extend([{"role": "assistant", "content": [{**tool, "input": json.loads(arguments)}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool["id"],
                                                  "content": "The local file was written."}]}])
    request["stream"] = False
    reply = json.loads(wire.chunk(run()))
    assert reply["content"][0]["text"] == "File confirmed."
    if provider == "openai":
        assert all(body.get("store") is False for _, body in upstream.requests)
    assert len(upstream.requests) == 2
    assert all(name == provider for name, _ in upstream.requests)
    assert all("Keep the edit local." in json.dumps(body) for _, body in upstream.requests)
    assert "PRIVATE" not in json.dumps(upstream.requests)
    assert "The local file was written." in json.dumps(upstream.requests[-1])


def test_relay_requires_current_receipt_before_any_model_data(tmp_path, monkeypatch):
    monkeypatch.setattr(relay.wire, "require_enabled", lambda: None)
    receipt = relay.enroll(tmp_path, {"provider": "openai", "claim_id": "b" * 32, "account_fp": "c" * 64,
                                     "model": "gpt-5.4", "binding_id": "d" * 32}, "fake-api-key-for-tests")
    seen = []
    def invoke(receipt):
        data = wire.preamble({"operation": "status", "route": receipt})
        body = json.dumps({"model": "fleet-gpt", "max_tokens": 20, "messages": [
            {"role": "user", "content": "test"}]}).encode()
        data += wire.preamble({"operation": "request", "method": "POST", "path": "/v1/messages",
                              "headers": {"content-type": "application/json"}, "body_size": len(body)}) + body
        async def create(**opts):
            seen.append(opts)
            return {"content": [{"type": "text", "text": "Hello"}]}
        output = io.BytesIO()
        result = relay.serve(io.BytesIO(data), output, tmp_path, create)
        return result, output.getvalue()
    for field in ("model", "account_fp", "claim_id", "credential_fp"):
        result, data = invoke({**receipt, field: "changed"})
        assert result == 2 and not seen and b"fake-api-key" not in data
    result, data = invoke(receipt)
    assert result == 0 and seen[0]["model"] == "openai/gpt-5.4"
    assert b"fake-api-key" not in data
    assert seen[0]["api_key"] == "fake-api-key-for-tests"
