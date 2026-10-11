"""Own-account HTTPS endpoint controls. Secrets appear only on the creation response."""
from __future__ import annotations

from collections.abc import Mapping
from html import escape
from typing import Any

from . import slots as slotstates
from .store import StoreError

MODES = {"sticky", "priority"}
NOTES = {
    "gateway-updated": ("ok", "Endpoint updated. New requests use your new slot selection."),
    "gateway-revoked": ("ok", "Endpoint key revoked. Applications using it can no longer connect."),
    "gateway-invalid": ("warn", "Choose at least one of your own slots, use distinct priorities, "
                        "and enter an endpoint name of 1 to 60 characters."),
}


def available(cfg: Any) -> bool:
    return getattr(cfg, "gateway_ready", False) is True


def _csrf(token: str) -> str:
    return '<input type="hidden" name="csrf" value="' + escape(token, quote=True) + '">'


def _choices(held: list[Mapping[str, Any]], selected: list[str]) -> str:
    rows = []
    positions = {identifier: index + 1 for index, identifier in enumerate(selected)}
    for slot in held:
        if slot.get("kind") != slotstates.MACHINE_SLOT or slot.get("state") not in (
                slotstates.CLAIMING, slotstates.CLAIMED, slotstates.ACTIVE):
            continue
        identifier = str(slot["id"])
        rows.append('<label class="gateway-slot"><span><b>'
                    + escape(str(slot.get("name") or identifier)) + '</b> <small>'
                    + escape({slotstates.CLAIMING: "Setting up",
                              slotstates.CLAIMED: "Needs sign-in",
                              slotstates.ACTIVE: "In use"}[slot["state"]]) + '</small></span>'
                    '<input type="number" min="1" max="100" step="1" name="slot.'
                    + escape(identifier, quote=True) + '" value="'
                    + str(positions.get(identifier, ""))
                    + '" aria-label="Priority for '
                    + escape(str(slot.get("name") or identifier), quote=True)
                    + '" placeholder="Unused"></label>')
    return ('<fieldset class="gateway-slots"><legend>Your slots in priority order</legend>'
            '<p class="small muted">Enter 1 for your first choice, 2 for your backup, and so on. '
            'Leave a slot blank to exclude it. For one specific account, select just one slot. '
            'With just one selected slot, there is no account switching. Slots still setting up '
            'can be selected but cannot handle requests yet.</p>'
            + ''.join(rows) + '</fieldset>')


def _routing(mode: str) -> str:
    return ('<label>Routing <select name="mode">'
            '<option value="sticky"' + (' selected' if mode == "sticky" else '')
            + '>Keep conversations on the same account (default)</option>'
            '<option value="priority"' + (' selected' if mode == "priority" else '')
            + '>Try priority order for each request</option></select></label>'
            '<p class="small muted">With multiple slots, the default keeps a conversation on '
            'the account that last succeeded while it remains available. API clients must send '
            'a stable session ID in <code>X-CCFleet-Session</code> or supported native metadata; '
            'without one, requests use priority order. A restart or a long idle period can reset '
            'that preference. Priority mode checks your order again for each request; recently '
            'failed accounts may be skipped briefly. Either mode can use a backup after an '
            'eligible failure.</p>')


def _form(action: str, csrf: str, held: list[Mapping[str, Any]], *,
          key: Mapping[str, Any] | None = None) -> str:
    creating = key is None
    selected = list(key.get("slot_ids") or []) if key else []
    identifier = ('<input type="hidden" name="key_id" value="'
                  + escape(str(key["id"]), quote=True) + '">') if key else ''
    return ('<form method="post" action="/account/gateway/' + action + '">'
            + _csrf(csrf) + identifier
            + '<label>Endpoint name <input name="name" maxlength="60" required value="'
            + escape(str(key.get("name", "")) if key else "Claude Code", quote=True)
            + '"></label>' + _choices(held, selected)
            + _routing(str(key.get("mode")) if key else "sticky")
            + '<p><button class="btn primary" type="submit">'
            + ('Create endpoint key' if creating else 'Save routing') + '</button></p></form>')


def panel(store: Any, cfg: Any, account: Mapping[str, Any], csrf: str,
          held: list[Mapping[str, Any]]) -> str:
    if not available(cfg):
        return ''
    keys = store.list_gateway_keys(account["id"])
    active = [key for key in keys if not key.get("revoked_at")]
    base_url = cfg.public_url.rstrip('/')
    entries = []
    for key in active:
        revoke = ('<form method="post" action="/account/gateway/revoke">' + _csrf(csrf)
                  + '<input type="hidden" name="key_id" value="'
                  + escape(str(key["id"]), quote=True) + '">'
                  '<button class="btn danger" type="submit">Revoke key</button></form>')
        entries.append('<details class="gateway-endpoint"><summary>'
                       + escape(str(key.get("name") or "Endpoint")) + ' · '
                       + ('one fixed account' if len(key.get('slot_ids') or []) == 1
                          else 'conversation routing' if key.get("mode") == 'sticky'
                          else 'priority routing')
                       + '</summary><p class="small muted">Key reference: <code>'
                       + escape(str(key['id'])) + '</code></p>'
                       + _form('update', csrf, held, key=key)
                       + revoke + '</details>')
    return ('<div class="card" id="gateway"><h2>API endpoints</h2>'
            '<p>Use original Claude Code or a compatible Messages API client directly over HTTPS. '
            'Your files and tools run locally; model requests travel through our gateway '
            'to your selected accounts.</p>'
            '<p><b>Base URL:</b> <code>' + escape(base_url) + '</code></p>'
            '<p class="small muted">Only slots you own can be selected. The gateway can try a '
            'backup for a confirmed eligible rejection or a connection failure before a model '
            'request was sent. It does not make another attempt when the outcome is unknown '
            'or after response headers have been sent. Applications may have their own retry '
            'behavior. Our gateway can read model requests in transit; your Claude account '
            'credential remains on its slot.</p>'
            + ''.join(entries) + '<details' + ('' if active else ' open')
            + '><summary>Create an endpoint</summary>' + _form('create', csrf, held) + '</details>'
            '<p>CC Fleet CLI: <code>ccfleet gateway setup --slots first-slot backup-slot '
            '--routing sticky</code>, '
            'then <code>ccfleet local</code>. Select only one slot for a fixed account, or use '
            '<code>ccfleet local --slot first-slot</code> to pin one selected slot '
            'for a launch, with no automatic switch to another account. A released or reclaimed '
            'slot needs its endpoint selection saved again before its new account can be used.</p>'
            '<style>.gateway-slot{display:flex;justify-content:space-between;gap:16px;'
            'align-items:center;margin:10px 0}'
            '.gateway-slot input{width:100px}.gateway-slots{border:1px solid var(--rule);'
            'border-radius:8px;margin:16px 0}'
            '.gateway-endpoint{margin:16px 0}#gateway summary{cursor:pointer;font-weight:600}'
            '#gateway label{display:block;margin:12px 0}#gateway .gateway-slot{display:flex}'
            '#gateway select{max-width:100%;width:100%}</style></div>')


def selected_slots(store: Any, account_id: str, form: Mapping[str, str]) -> list[str]:
    held = {slot["id"]: slot for slot in store.list_slots(held_by=account_id)
            if slot.get("kind") == slotstates.MACHINE_SLOT and slot.get("state") in (
                slotstates.CLAIMING, slotstates.CLAIMED, slotstates.ACTIVE)}
    choices = []
    for field, value in form.items():
        if not field.startswith('slot.') or value == '':
            continue
        identifier = field[5:]
        if identifier not in held or not value.isascii() or not value.isdigit():
            raise ValueError("invalid selection")
        priority = int(value)
        if not 1 <= priority <= 100:
            raise ValueError("invalid priority")
        choices.append((priority, identifier))
    if not choices or len({number for number, _ in choices}) != len(choices):
        raise ValueError("choose slots with unique priorities")
    return [identifier for _, identifier in sorted(choices)]


def act(store: Any, cfg: Any, account: Mapping[str, Any], path: str,
        form: Mapping[str, str], now: float, session_id: str = "") -> Any:
    # Imported at call time so usersite can use the panel without a cycle.
    from . import usersite
    viewer = usersite.viewer_for(account, session_id, cfg, store)
    if not available(cfg):
        return usersite.not_found(viewer)
    action = path.rsplit('/', 1)[-1]
    try:
        if action == 'revoke':
            store.revoke_gateway_key(form.get("key_id", ""), account["id"], now=now)
            return usersite._back("gateway-revoked", "gateway")
        if action not in {'create', 'update'} or form.get("mode") not in MODES:
            raise ValueError("invalid action")
        slot_ids = selected_slots(store, account["id"], form)
        if action == 'update':
            store.update_gateway_key(form.get("key_id", ""), account["id"], slot_ids,
                                     form["mode"], name=form.get("name", ""), now=now)
            return usersite._back("gateway-updated", "gateway")
        key = store.create_gateway_key(account["id"], form.get("name", ""), slot_ids,
                                      mode=form["mode"], now=now)
    except (StoreError, ValueError, TypeError):
        return usersite._back("gateway-invalid", "gateway")
    body = ('<div class="pagehead"><h1>Your endpoint key</h1></div><div class="card">'
            '<p>This key is shown once. Save it in your password manager; the server keeps '
            'only its hash. Never share it or commit it to a project.</p><pre class="token">'
            + escape(str(key["key_token"])) + '</pre><p><b>Base URL:</b> <code>'
            + escape(cfg.public_url.rstrip('/')) + '</code></p>'
            '<p>For native Claude Code, set <code>ANTHROPIC_BASE_URL</code> to this URL '
            'and <code>ANTHROPIC_AUTH_TOKEN</code> to this endpoint key. Keep the key in '
            'private local settings, outside your project. The endpoint supports '
            '<code>POST /v1/messages</code> and <code>POST /v1/messages/count_tokens</code>.</p>'
            '<p>For guided setup without displaying a key in your terminal, use '
            '<code>ccfleet gateway setup</code> on an already paired computer, then '
            '<code>ccfleet local</code>.</p>'
            '<p><a href="/account#gateway">Back to API endpoints</a></p></div>')
    return usersite.Outcome(200, body=usersite._shell("Your endpoint key", body, viewer=viewer))
