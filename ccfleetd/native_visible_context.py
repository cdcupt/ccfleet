"""Explicit lossy projection of private assistant reasoning into visible context.

Only the reviewed Devin native-agent bridge uses this; raw Anthropic Messages
and unknown native profiles retain their original behavior. This is not a
transfer of signed reasoning or a claim of model/role semantic parity.
"""

from ccfleet_agent import devin_native as native

OMISSION_NOTE = "[Fleet Devin compatibility note: private assistant reasoning omitted.]"


def _private_block(block):
    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    fields = ({"type", "thinking", "signature"} if kind == "thinking" else
              {"type", "data"} if kind == "redacted_thinking" else None)
    if (fields is None or not fields <= block.keys()
            or block.keys() - (fields | {"cache_control"})
            or any(not isinstance(block[name], str) for name in fields)):
        return False
    try:
        native._cache(block)
    except native.NativeError:
        return False
    return True


def visible_history(body):
    """Bound the original request first; preserve visible objects and turn order.

    Unknown/malformed shapes remain present so the unchanged native validator
    rejects them. No private block in user/system/nested tool content is removed.
    A fixed bridge note retains the position of a private-only assistant turn.
    """
    native._encode(body)
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return body
    changed, messages = False, []
    for message in body["messages"]:
        if (not isinstance(message, dict) or message.get("role") != "assistant"
                or not isinstance(message.get("content"), list)):
            messages.append(message)
            continue
        content = [block for block in message["content"] if not _private_block(block)]
        if len(content) != len(message["content"]):
            changed = True
            messages.append({**message, "content": content or [
                {"type": "text", "text": OMISSION_NOTE}
            ]})
        else:
            messages.append(message)
    return {**body, "messages": messages} if changed else body
