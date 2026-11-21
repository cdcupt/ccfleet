"""Owner-only browser setup for the private Mac's Gemini API credential."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid

from . import gemini_setup_store as data
from . import gemini_setup_ui as ui
from .bilingual import label
from .gateway_transport import ApiModelAttempt, response_metadata
from .gateway_transport import wire as protocol

PATH = ui.PATH
ACTIONS = {PATH + "/" + action for action in ("start", "finish", "disconnect")}
CAPACITY = threading.BoundedSemaphore(4)

# The input containing the actual key has no form name. A browser without this
# script cannot serialize it. Only the compact JWE is submitted to Fleet.
SCRIPT = """(() => {
  const copy = document.getElementById("gemini-copy");
  if (copy && navigator.clipboard) {
    copy.hidden = false;
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(document.getElementById("gemini-code").textContent);
        document.getElementById("copy-status").textContent =
          document.documentElement.lang.startsWith("zh") ? "已复制" : "Copied";
      } catch {
        document.getSelection().selectAllChildren(document.getElementById("gemini-code"));
      }
    });
  }
  const form = document.getElementById("gemini-credential");
  if (!form || !window.crypto?.subtle) return;
  document.getElementById("crypto-required").hidden = true;
  const key = document.getElementById("gemini-key");
  const button = form.querySelector("button");
  const error = form.querySelector("[role=alert]");
  const encode = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)))
    .replaceAll("+","-").replaceAll("/","_").replace(/=+$/,"");
  const decode = s => Uint8Array.from(atob(s.replaceAll("-","+").replaceAll("_","/")),
    c => c.charCodeAt(0));
  const utf8 = new TextEncoder();
  button.disabled = false;
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    try {
      const protectedHeader = {alg:"RSA-OAEP-256",enc:"A256GCM",kid:form.dataset.nonce};
      const header = encode(utf8.encode(JSON.stringify(protectedHeader)));
      const wrap = await crypto.subtle.importKey("spki",decode(form.dataset.pub),
        {name:"RSA-OAEP",hash:"SHA-256"},false,["encrypt"]);
      const aes = await crypto.subtle.generateKey({name:"AES-GCM",length:256},true,["encrypt"]);
      const iv = crypto.getRandomValues(new Uint8Array(12));
      const cipher = new Uint8Array(await crypto.subtle.encrypt(
        {name:"AES-GCM",iv,additionalData:utf8.encode(header)},aes,utf8.encode(key.value)));
      const aesBytes = await crypto.subtle.exportKey("raw",aes);
      const wrapped = await crypto.subtle.encrypt({name:"RSA-OAEP"},wrap,aesBytes);
      form.elements.envelope.value = [header,encode(wrapped),encode(iv),
        encode(cipher.slice(0,-16)),encode(cipher.slice(-16))].join(".");
      key.value = "";
      HTMLFormElement.prototype.submit.call(form);
    } catch {
      error.hidden = false;
      button.disabled = false;
    }
  });
})();"""
SCRIPT_CSP = "'sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode() + "'"


class Unavailable(ValueError):
    pass


def remote(slot, cfg, request):
    import time

    if not CAPACITY.acquire(blocking=False):
        raise Unavailable("busy")
    attempt = ApiModelAttempt(slot, cfg)
    timer = threading.Timer(15, attempt.close)
    timer.daemon = True
    started = time.monotonic()
    try:
        attempt.process = attempt._spawn(attempt._command())
        timer.start()
        attempt.process.stdin.write(
            protocol.preamble({"operation": "gemini-web", "request": request})
        )
        attempt.process.stdin.flush()
        meta = response_metadata(attempt.process.stdout)
        raw = bytearray()
        for part in attempt.chunks():
            raw.extend(part)
            if len(raw) > 4096:
                raise Unavailable("oversized Mac reply")
        attempt.finish(meta["status"], timeout=max(0.1, 15 - (time.monotonic() - started)))
        if meta["status"] != 200:
            raise Unavailable("Mac refused setup")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise Unavailable("invalid Mac reply")
        return result
    except Exception as exc:
        raise Unavailable("Mac unavailable") from exc
    finally:
        timer.cancel()
        attempt.close()
        CAPACITY.release()


def flow_signature(raw, session, cfg):
    message = b"gemini-enroll-v1\0" + session.encode() + b"\0" + raw.encode()
    return hmac.new(cfg.cookie_secret.encode(), message, hashlib.sha256).hexdigest()


def sign_flow(flow, session, cfg):
    raw = base64.urlsafe_b64encode(json.dumps(flow, separators=(",", ":")).encode()).decode()
    return raw + "." + flow_signature(raw, session, cfg)


def verify_flow(token, session, cfg, now):
    if not isinstance(token, str) or len(token) > 2400:
        raise data.SetupError("invalid flow")
    raw, _, signature = token.partition(".")
    if not hmac.compare_digest(flow_signature(raw, session, cfg), signature):
        raise data.SetupError("invalid flow")
    flow = json.loads(base64.b64decode(raw, altchars=b"-_", validate=True))
    if not isinstance(flow, dict) or not now < flow["expires"] <= now + 300:
        raise data.SetupError("expired or invalid flow")
    return flow


def page(store, cfg, account, session, *, note="", status=200, secret_form=""):
    from . import usersite

    viewer = usersite.viewer_for(account, session, cfg, store)
    slot, keys, binding, grants = store.gemini_setup_state(account["id"])
    if secret_form:
        main = ui.panel(ui.tr("Save your API key", "保存 API 密钥"), secret_form)
    else:
        fields = ""
        if not binding or not data.retiring(binding):
            fields = ui.setup_form(viewer.csrf, keys, binding)
        if binding:
            fields += ui.disconnect_form(viewer.csrf, binding)
        main = ui.panel(ui.tr("Computer connection", "电脑连接"), fields)
    body = ui.shell(ui.tr("Gemini API connection", "Gemini API 连接"), ui.tr(
        "Connect Gemini to local Claude Code. Usage is billed to your Google Cloud project.",
        "为本地 Claude Code 连接 Gemini API。用量计入你的 Google Cloud 项目。"),
        main, ui.slot_panel(slot, keys, binding, grants), viewer, note=note,
        back="/account#slot-" + slot["id"])
    return usersite.Outcome(status, body=body)


def act(store, cfg, account, session, action, fields, now, transport=None):
    from . import usersite

    transport = remote if transport is None else transport
    owner = account["id"]
    csrf = usersite.csrf_for(session, cfg.cookie_secret)

    def ok():
        return usersite.Outcome(303, location=PATH)

    try:
        if action == "disconnect":
            if set(fields) != {"csrf", "binding", "confirm"} or fields["confirm"] != "1":
                raise data.SetupError("confirmation required")
            slot, route = store.gemini_setup_disconnect(owner, fields["binding"])
            result = transport(
                slot, cfg, {"action": "forget", "reservation": route, "nonce": uuid.uuid4().hex}
            )
            if result != {"removed": True}:
                raise Unavailable("Mac cleanup pending")
            store.gemini_setup_disconnect(owner, fields["binding"], completed=True)
            return ok()
        if action == "finish":
            if set(fields) != {"csrf", "flow", "envelope"} or len(fields["envelope"]) > 6000:
                raise data.SetupError("invalid credential form")
            flow = verify_flow(fields["flow"], session, cfg, now)
            slot = store.gemini_setup_continue(owner, flow)
            answer = transport(
                slot,
                cfg,
                {
                    "action": "finish",
                    "nonce": flow["nonce"],
                    "reservation": flow["route"],
                    "envelope": fields["envelope"],
                },
            )
        elif action == "start":
            if set(fields) not in ({"csrf", "key"}, {"csrf", "key", "project", "model"}):
                raise data.SetupError("invalid start")
            if "model" in fields and not re.fullmatch(
                r"gemini-[A-Za-z0-9._-]{1,88}", fields["model"]
            ):
                raise data.SetupError("invalid model")
            key = fields["key"]
            slot, route, generation, pinned = store.gemini_setup_start(
                owner, key, fields.get("project", ""), fields.get("model", "")
            )
            flow = {
                "key": key,
                "route": route,
                "generation": generation,
                "nonce": uuid.uuid4().hex,
                "expires": int(now) + 300,
            }
            answer = transport(
                slot,
                cfg,
                {
                    "action": "prepare",
                    "reservation": route,
                    "nonce": flow["nonce"],
                    "pinned": pinned,
                },
            )
            if set(answer) == {"public_key"}:
                if not isinstance(answer["public_key"], str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]{400,900}", answer["public_key"]
                ):
                    raise Unavailable("invalid public key")
                opened = ui.credential_form(csrf, sign_flow(flow, session, cfg), flow["nonce"],
                                            answer["public_key"], SCRIPT)
                return page(store, cfg, account, session, secret_form=opened)
        else:
            raise data.SetupError("unknown action")
        if set(answer) != {"receipt"} or not isinstance(answer["receipt"], dict):
            raise Unavailable("unexpected reply")
        result = store.gemini_setup_continue(owner, flow, answer["receipt"], now=now)
        if flow["key"] == "new":
            viewer = usersite.viewer_for(account, session, cfg, store)
            state = store.gemini_setup_state(owner)
            return usersite.Outcome(200, body=ui.pairing_result(result, *state, viewer, SCRIPT))
        return ok()
    except Unavailable:
        return page(
            store,
            cfg,
            account,
            session,
            status=503,
            note=label(
                "The Mac has not confirmed this step yet. Keep it awake and retry in a minute.",
                "Mac 尚未确认。请保持其唤醒，一分钟后重试。",
                inline=True,
            ),
        )
    except (ValueError, KeyError, TypeError):
        return page(
            store,
            cfg,
            account,
            session,
            status=409,
            note=label(
                "Setup expired or the connection details changed. Start setup again.",
                "设置已过期或槽位、项目或连接已变更。请核对并重试。",
                inline=True,
            ),
        )
