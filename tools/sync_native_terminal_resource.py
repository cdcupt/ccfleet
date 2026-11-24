"""Pack the canonical terminal source into the backward-compatible signed CLI bundle."""
import base64
import hashlib
import re
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    source = (ROOT / "ccfleet_agent/gemini_native_terminal.py").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    launcher = ROOT / "laptop/ccfleet"
    text = launcher.read_text()
    pin = f'NATIVE_TERMINAL_HASH = "{digest}"'
    text, pins = re.subn(r'^NATIVE_TERMINAL_HASH = "[a-f0-9]{64}"$', pin, text, flags=re.M)
    lines = textwrap.wrap(base64.b64encode(source).decode("ascii"), width=92)
    body = "".join(f'    b"{line}"\n' for line in lines)
    resource = "NATIVE_TERMINAL_RESOURCE = (\n" + body + ")"
    marker = (r"(?<=# BEGIN generated native terminal resource\n).*?"
              r"(?=\n# END generated native terminal resource)")
    text, blocks = re.subn(marker, resource, text, flags=re.S)
    if (pins, blocks) != (1, 1):
        raise ValueError("Could not locate the unique terminal pin and resource in the launcher")
    launcher.write_text(text)


if __name__ == "__main__":
    main()
