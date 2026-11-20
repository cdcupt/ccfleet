"""Non-secret enrollment contract shared by the controller and the slot."""
import hashlib
import re

ALIASES = {"openai": "fleet-gpt", "gemini": "fleet-gemini"}
RESERVATION = ("provider", "model", "claim_id", "account_fp", "binding_id")
FIELDS = (*RESERVATION, "credential_fp")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def account_fingerprint(provider, scope):
    pattern = {"openai": r"org-[A-Za-z0-9_-]+", "gemini": r"[1-9][0-9]*"}.get(provider)
    if (not pattern or not isinstance(scope, str) or len(scope) > 128
            or not re.fullmatch(pattern, scope)):
        raise ValueError("use the verified OpenAI organization ID or Google Cloud project number")
    return digest(scope)


def validate(value, *, credential=True):
    fields = FIELDS if credential else RESERVATION
    if (not isinstance(value, dict) or set(value) != set(fields)
            or not all(isinstance(v, str) for v in value.values())
            or value["provider"] not in ALIASES
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", value["model"])
            or not all(re.fullmatch(r"[a-f0-9]{32}", value[k]) for k in ("claim_id", "binding_id"))
            or not re.fullmatch(r"[a-f0-9]{64}", value["account_fp"])
            or credential and not re.fullmatch(r"[a-f0-9]{64}", value["credential_fp"])):
        raise ValueError("invalid provider enrollment receipt")
