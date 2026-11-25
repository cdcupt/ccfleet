"""Build the downloadable connector from the same sources shipped in the controller."""

import base64
import functools
import io
import json
import sysconfig
import zipfile
from importlib import resources
from pathlib import Path

MODULES = (
    "home_mac_runtime",
    "mac_owner_agent",
    "mac_policy",
    "mac_credentials",
    "mac_keys",
    "mac_native_probe",
    "mac_slot_entry",
    "mac_relay_client",
    "mac_relay_service",
    "mac_relay_worker",
    "local_relay",
    "inference_policy",
    "relay_metrics",
    "agent",
    "egress",
    "compatibility",
    "model_usage",
)


@functools.lru_cache(maxsize=2)
def build(origin):
    package = resources.files("ccfleet_agent")
    cli = Path(__file__).resolve().parents[1] / "laptop/ccfleet"
    if not cli.is_file():
        cli = Path(sysconfig.get_path("data")) / "share/ccfleet/home-mac/ccfleet"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        files = {name + ".py": package.joinpath(name + ".py").read_bytes() for name in MODULES}
        files["fleet_wire.py"] = cli.read_bytes()
        for name, contents in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            archive.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED)
    source = (
        package.joinpath("home_mac.py")
        .read_text()
        .replace('ORIGIN = "https://ccfleet.io"', "ORIGIN = " + repr(origin), 1)
    )
    return source.replace(
        'BUNDLE = ""',
        "BUNDLE = " + json.dumps(base64.b64encode(output.getvalue()).decode("ascii")),
        1,
    ).encode()


# Same public verification identity as the signed Fleet laptop installer.
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJAfp/o543ltpXhS4vumv2mParGmmLV8mQ42xW9usGhn"
COMMAND = "ccfleet-home-mac.command"
MANIFEST = "ccfleet-home-mac.json"
SIGNATURE = MANIFEST + ".sig"


@functools.lru_cache(maxsize=4)
def verified_assets(directory, origin):
    import hashlib

    from ccfleet_agent.client_release import verify_signature

    if not directory:
        raise ValueError("No signed connector release is configured.")
    root = Path(directory)
    contents = {}
    for name, maximum in ((MANIFEST, 4096), (SIGNATURE, 2048), (COMMAND, 4 * 1024 * 1024)):
        with (root / name).open("rb") as source:
            contents[name] = source.read(maximum + 1)
        if not 0 < len(contents[name]) <= maximum:
            raise ValueError("Invalid connector release size.")
    verify_signature(contents[MANIFEST], contents[SIGNATURE], PUBLIC_KEY)
    manifest = json.loads(contents[MANIFEST])
    if (
        not isinstance(manifest, dict)
        or manifest.get("kind") != "home-mac"
        or manifest.get("version") != 1
        or manifest.get("origin") != origin
        or manifest.get("file") != COMMAND
        or manifest.get("sha256") != hashlib.sha256(contents[COMMAND]).hexdigest()
    ):
        raise ValueError("The connector does not match its signed release.")
    return contents
