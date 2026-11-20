"""Owner-only browser setup for the private Mac's Gemini API credential."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid
from html import escape

from . import gemini_setup_store as data
from .bilingual import label
from .gateway_transport import ApiModelAttempt, response_metadata
from .gateway_transport import wire as protocol

PATH = "/account/gemini"
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


def hidden(name, value):
    return '<input type="hidden" name="' + name + '" value="' + escape(value, quote=True) + '">'


def form(action, csrf, content, button):
    return (
        f'<form method="post" action="{PATH}/{action}">'
        + hidden("csrf", csrf)
        + content
        + '<button type="submit">'
        + button
        + "</button></form>"
    )


def page(store, cfg, account, session, *, note="", status=200, secret_form=""):
    from . import usersite

    viewer = usersite.viewer_for(account, session, cfg, store)
    slot, keys, binding, grants = store.gemini_setup_state(account["id"])
    title = label("Gemini on your Mac", "在 Mac 上使用 Gemini", inline=True)
    content = (
        '<section class="card"><h1>'
        + title
        + "</h1><p>"
        + escape(slot["name"] or slot["id"])
        + "</p>"
    )
    if note:
        content += '<p role="status">' + note + "</p>"
    if secret_form:
        content += secret_form
    else:
        if binding:
            content += (
                "<p>"
                + label("Model", "模型", inline=True)
                + " · "
                + escape(binding["model"])
                + "</p>"
            )
            if grants:
                content += (
                    "<p>"
                    + label("Connected computers", "已连接的电脑", inline=True)
                    + " · "
                    + ", ".join(escape(keys[k]["name"]) for k in keys if k in grants)
                    + "</p>"
                )
        if not binding or not data.retiring(binding):
            options = "".join(
                '<option value="'
                + escape(k, quote=True)
                + '">'
                + escape(v["name"])
                + " · "
                + escape(k[:9])
                + "</option>"
                for k, v in keys.items()
            )
            options = (
                '<option value="new" data-copy-en="New MacBook · get a pairing code" '
                'data-copy-zh="新电脑 · 获取配对码">New MacBook · get a pairing code</option>'
                + options
            )
            fields = (
                "<label>"
                + label("Computer connection", "电脑连接", inline=True)
                + '<select name="key">'
                + options
                + "</select></label>"
            )
            if not binding:
                fields += (
                    "<label>"
                    + label(
                        "Google Cloud project number that owns the API key",
                        "API 密钥所属的 Google Cloud 项目编号",
                        inline=True,
                    )
                    + '<input required inputmode="numeric" name="project" pattern="[1-9][0-9]*"'
                    ' maxlength="30"></label>'
                    + "<label>"
                    + label("Gemini model ID", "Gemini 模型 ID", inline=True)
                    + '<input required name="model" '
                    'pattern="gemini-[A-Za-z0-9._\\-]+" maxlength="95"></label>'
                )
            content += form(
                "start", viewer.csrf, fields, label("Connect Gemini", "连接 Gemini", inline=True)
            )
        if binding:
            content += form(
                "disconnect",
                viewer.csrf,
                hidden("binding", binding["id"])
                + '<label><input type="checkbox" required name="confirm" value="1">'
                + label(
                    "Remove Gemini from this slot and its linked computers.",
                    "从此槽位及关联电脑移除 Gemini。",
                    inline=True,
                )
                + "</label>",
                label("Disconnect Gemini", "断开 Gemini", inline=True),
            )
    content += (
        '<p><a href="/account#slot-'
        + escape(slot["id"], quote=True)
        + '">'
        + label("Back to my slot", "返回我的槽位", inline=True)
        + "</a></p></section>"
    )
    return usersite.Outcome(status, body=usersite._shell("Gemini", content, viewer=viewer))


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
                opened = (
                    "<p>"
                    + label(
                        "Your browser encrypts the key for your Mac. "
                        "Usage goes to your Google Cloud project.",
                        "浏览器会为 Mac mini 加密密钥。Gemini API 用量计入所选 Google Cloud 项目。",
                        inline=True,
                    )
                    + "</p>"
                    + f'<form method="post" id="gemini-credential" action="{PATH}/finish"'
                    + f' data-nonce="{flow["nonce"]}" data-pub="{answer["public_key"]}">'
                    + hidden("csrf", csrf)
                    + hidden("flow", sign_flow(flow, session, cfg))
                    + hidden("envelope", "")
                    + "<label>"
                    + label("Gemini API key", "Gemini API 密钥", inline=True)
                    + '<input type="password" required minlength="16" maxlength="2048" '
                    'id="gemini-key" autocomplete="off"></label>'
                    + '<p id="crypto-required">'
                    + label(
                        "Use a browser with JavaScript and encrypted connection support.",
                        "请使用支持 JavaScript 和安全连接的浏览器。",
                        inline=True,
                    )
                    + "</p>"
                    + '<button disabled type="submit">'
                    + label("Save on my Mac", "保存到我的 Mac", inline=True)
                    + '</button><p role="alert" hidden>'
                    + label(
                        "Encryption failed. Reopen setup and try again.",
                        "加密失败，请重新打开设置再试。",
                        inline=True,
                    )
                    + "</p></form><script>"
                    + SCRIPT
                    + "</script>"
                )
                return page(store, cfg, account, session, secret_form=opened)
        else:
            raise data.SetupError("unknown action")
        if set(answer) != {"receipt"} or not isinstance(answer["receipt"], dict):
            raise Unavailable("unexpected reply")
        result = store.gemini_setup_continue(owner, flow, answer["receipt"], now=now)
        if flow["key"] == "new":
            viewer = usersite.viewer_for(account, session, cfg, store)
            content = (
                '<section class="card"><h1>'
                + label("Your Gemini pairing code", "Gemini 配对码", inline=True)
                + "</h1><p>"
                + label(
                    "Paste this code into the Fleet pairing prompt on your MacBook. "
                    "It works once and expires in 10 minutes.",
                    "在 MacBook 的 Fleet 配对提示中粘贴此码。配对码仅可使用一次，10 分钟后过期。",
                    inline=True,
                )
                + '</p><pre class="token" id="gemini-code">'
                + escape(result)
                + '</pre><button type="button" id="gemini-copy" hidden>'
                + label("Copy code", "复制配对码", inline=True)
                + '</button><span id="copy-status" role="status"></span>'
                + "<p>"
                + label(
                    "After pairing, choose Gemini · API in Claude Code’s /model menu.",
                    "完成配对后，在 Claude Code 的 /model 菜单选择 Gemini · API。",
                    inline=True,
                )
                + '</p><a href="/account/gemini">'
                + label("Back to Gemini", "返回 Gemini", inline=True)
                + "</a></section>"
                + "<script>"
                + SCRIPT
                + "</script>"
            )
            return usersite.Outcome(200, body=usersite._shell("Gemini", content, viewer=viewer))
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
