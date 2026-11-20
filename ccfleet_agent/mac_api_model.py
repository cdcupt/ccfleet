"""Private Mac API credentials stay in the owner's login Keychain and GUI worker."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import threading
from pathlib import Path


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = sibling("mac_policy")
contract = sibling("api_model_contract")
SERVICE = "CCFleet API models"


def authorize(home):
    policy.require_enabled(home)
    access = policy.read_json(home, "grants.json")["access"]
    if len(access.get("gateway_public_keys", [])) != 1:
        raise ValueError("private gateway authorization is unavailable")


def credential_store():
    from keyring.backends.macOS import Keyring

    return Keyring()


def save_profile(reservation, secret, store):
    contract.validate(reservation, credential=False)
    if not isinstance(secret, str) or not secret.isascii() or not 16 <= len(secret) <= 2048 \
            or any(ord(c) <= 32 or ord(c) == 127 for c in secret):
        raise ValueError("invalid API credential")
    # A new reservation uses a new account name; an old receipt cannot unlock it.
    profile = {**reservation, "api_key": secret}
    store.set_password(SERVICE, reservation["binding_id"], json.dumps(profile))
    return {**reservation, "credential_fp": contract.digest(secret)}


def read_profile(expected, store):
    contract.validate(expected)
    saved = store.get_password(SERVICE, expected["binding_id"])
    if not saved or len(saved) > 8192:
        raise ValueError("enroll the API credential on this Mac")
    value = json.loads(saved)
    if set(value) != {*contract.RESERVATION, "api_key"} or (
            {**{k: value[k] for k in contract.RESERVATION},
             "credential_fp": contract.digest(value["api_key"])} != expected):
        raise ValueError("private API model enrollment has changed")
    return value


def run(home, *, initial=None):
    authorize(home)
    relay = sibling("api_model_relay")
    os.chdir("/")
    os.environ.clear()
    os.environ.update(PATH=os.defpath, HOME=str(home), LITELLM_LOCAL_MODEL_COST_MAP="True",
                      LITELLM_MODE="PRODUCTION", LITELLM_LOG="ERROR")
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    signal.alarm(relay.wire.INPUT_TIMEOUT)
    import litellm

    litellm.suppress_debug_info = True
    litellm.num_retries = 0
    store = credential_store()

    def after_body():
        signal.alarm(relay.wire.REQUEST_TIMEOUT)

        def watch_grant():
            while not done.wait(1):
                try:
                    authorize(home)
                except (OSError, ValueError):
                    os._exit(2)
        threading.Thread(target=watch_grant, daemon=True).start()

    done = threading.Event()
    try:
        return relay.serve(sys.stdin.buffer, output, home, litellm.anthropic.messages.acreate,
                           after_body=after_body, authorize=lambda: authorize(home),
                           load=lambda _home, expected: read_profile(expected, store),
                           initial=initial)
    finally:
        done.set()
        signal.alarm(0)
