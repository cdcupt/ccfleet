"""One browser enrollment at a time, decrypted only in the private Mac GUI worker."""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import stat
import time

WRAP_SERVICE = "CCFleet Gemini browser enrollment"
PENDING = "pending"


def b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64(value, limit):
    if (
        not isinstance(value, str)
        or len(value) > limit * 2
        or not re.fullmatch(r"[A-Za-z0-9_-]+", value)
    ):
        raise ValueError("invalid encrypted field")
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(raw) > limit or b64(raw) != value:
        raise ValueError("invalid encrypted field")
    return raw


@contextlib.contextmanager
def exclusive(home, policy):
    directory = policy.open_directory(policy.directory(home), private=True)
    try:
        fd = os.open(
            "gemini-web.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600, dir_fd=directory
        )
    finally:
        os.close(directory)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_nlink != 1
        ):
            raise ValueError("invalid enrollment lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def decrypt(key, nonce, envelope):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(envelope, str) or len(envelope) > 6000:
        raise ValueError("invalid encrypted credential")
    head, wrapped, iv, body, tag = envelope.split(".")
    header = json.loads(unb64(head, 256))
    if header != {"alg": "RSA-OAEP-256", "enc": "A256GCM", "kid": nonce}:
        raise ValueError("enrollment context changed")
    private = serialization.load_pem_private_key(key.encode("ascii"), password=None)
    aes_key = private.decrypt(
        unb64(wrapped, 384),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    iv, tag = unb64(iv, 12), unb64(tag, 16)
    if len(aes_key) != 32 or len(iv) != 12 or len(tag) != 16:
        raise ValueError("invalid encryption parameters")
    return AESGCM(aes_key).decrypt(iv, unb64(body, 2048) + tag, head.encode()).decode("ascii")


def action(api, store, request, now):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    mode = request["action"]
    reservation = request["reservation"]
    api.contract.validate(reservation, credential=False)
    if reservation["provider"] != "gemini":
        raise ValueError("unsupported provider")
    nonce = request["nonce"]
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{32}", nonce):
        raise ValueError("invalid enrollment identifier")
    binding = reservation["binding_id"]
    existing = store.get_password(api.SERVICE, binding)
    if mode == "forget":
        if existing:
            saved = json.loads(existing)
            if {k: saved.get(k) for k in api.contract.RESERVATION} != reservation:
                raise ValueError("Mac binding changed")
            store.delete_password(api.SERVICE, binding)
        pending = store.get_password(WRAP_SERVICE, PENDING)
        if pending and json.loads(pending)["reservation"] == reservation:
            store.delete_password(WRAP_SERVICE, PENDING)
        return {"removed": True}
    if mode == "prepare":
        pinned = request.get("pinned")
        if pinned is not None and not re.fullmatch(r"[a-f0-9]{64}", pinned):
            raise ValueError("invalid credential fingerprint")
        if existing:
            saved = json.loads(existing)
            credential = saved.pop("api_key")
            if saved != reservation or (pinned and pinned != api.contract.digest(credential)):
                raise ValueError("Mac binding changed")
            return {"receipt": {**saved, "credential_fp": api.contract.digest(credential)}}
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        private = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        store.set_password(
            WRAP_SERVICE,
            PENDING,
            json.dumps(
                {
                    "nonce": nonce,
                    "reservation": reservation,
                    "expires": int(now) + 300,
                    "pinned": pinned,
                    "key": private,
                }
            ),
        )
        der = key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return {"public_key": b64(der)}
    if mode != "finish" or existing:
        raise ValueError("start a fresh enrollment")
    state = json.loads(store.get_password(WRAP_SERVICE, PENDING) or "{}")
    if (
        state.get("nonce") != nonce
        or state.get("reservation") != reservation
        or not now < state.get("expires", 0) <= now + 300
    ):
        raise ValueError("enrollment expired or was replaced")
    secret = decrypt(state["key"], nonce, request.get("envelope"))
    if state.get("pinned") and api.contract.digest(secret) != state["pinned"]:
        raise ValueError("disconnect the previous credential before replacing it")
    result = api.save_profile(reservation, secret, store)
    store.delete_password(WRAP_SERVICE, PENDING)
    return {"receipt": result}


def handle(api, home, meta, relay, output):
    out = relay.wire._GatewayOutput(output)
    try:
        api.authorize(home)
        if set(meta) != {"version", "operation", "request"}:
            raise ValueError("invalid control message")
        with exclusive(home, api.policy):
            api.authorize(home)
            result = action(api, api.credential_store(), meta["request"], time.time())
        relay.wire.write_metadata(out, 200, {"content-type": "application/json"})
        relay.wire.write_chunk(out, json.dumps(result).encode())
        relay.wire.write_chunk(out, b"")
        return 0
    except Exception:
        relay.wire.send_error(out, 409, "Gemini setup could not complete on the assigned Mac.")
        return 2
