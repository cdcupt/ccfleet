"""Finite public response-model keys and standard API-equivalent token prices.

Verified against official pricing/model documentation on 2026-10-14. This module
is also loaded as a root-owned sibling under Python -I: keep it standalone.
Unknown identifiers never escape normalize(). Prices are not subscription bills.
"""
import re

PRICING_DATE = "2026-10-14"
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
MODELS_URL = "https://platform.claude.com/docs/en/models/overview"

# USD per million tokens: input, output, 5m cache write, 1h cache write, cache read.
# The per-request Haiku 5.5 context tier is not in the aggregate, so do not price it.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 8.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 4.0, 0.10),
    "claude-haiku-5-5": None,
    "claude-mythos-5-1": (10.0, 50.0, 12.5, 20.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 12.5, 20.0, 1.0),
    "claude-opus-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 6.25, 10.0, 0.50),
    "claude-sonnet-5": (2.0, 10.0, 2.5, 4.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 3.75, 6.0, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 2.0, 0.10),
    "other": None,
}
PUBLIC_MODELS = tuple(PRICES)
MODEL_KEYS = PUBLIC_MODELS
LABELS = {key: "Claude " + key.split("-")[1].title() + " " + ".".join(key.split("-")[2:])
          for key in PUBLIC_MODELS if key != "other"}
LABELS["other"] = "Other / unknown"
_SNAPSHOT_ID = re.compile(r"(.+)-[0-9]{8}\Z")


def normalize(value):
    """Return a catalogue key, never a raw request/provider label or identifier."""
    if not isinstance(value, str) or len(value) > 80:
        return "other"
    if value in PUBLIC_MODELS:
        return value
    match = _SNAPSHOT_ID.fullmatch(value)
    if match and match[1] in PUBLIC_MODELS:
        return match[1]
    return "other"
