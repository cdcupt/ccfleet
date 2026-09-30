"""The fixed public HTML reference library; never a general static-file server."""

from __future__ import annotations

import os
import stat
import sysconfig
from pathlib import Path
from typing import Optional

ROUTE_PREFIX = "/docs/library/"
INDEX_PATH = ROUTE_PREFIX + "docs/index.html"
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
PUBLIC_FILES = frozenset({
    "README.html",
    "gateway/README.html",
    *("docs/" + name + ".html" for name in (
        "index", "guidebook", "compliance", "design", "runbooks", "tunnel",
        "local-relay", "reliability-privacy", "platform-comparison", "platform-improvements",
        "local-relay-verification", "live-folders-verification", "project-workspaces",
        "project-workspaces-verification",
    )),
})


def library_route(path: str) -> bool:
    return path == ROUTE_PREFIX.rstrip("/") or path.startswith(ROUTE_PREFIX)


def relative_path(path: str) -> Optional[str]:
    """Only literal canonical URLs, with no decoding, normalization or traversal."""
    if not path.startswith(ROUTE_PREFIX):
        return None
    name = path[len(ROUTE_PREFIX):]
    return name if name in PUBLIC_FILES else None


def candidate_roots() -> tuple[Path, ...]:
    """Checkout, pip --target layout, then the interpreter installation's data."""
    # Canonicalize only trusted runtime anchors (e.g. macOS /tmp -> /private/tmp),
    # not the appended public-data tree or any requested document path.
    package_parent = Path(__file__).resolve().parent.parent
    roots = [package_parent, package_parent / "share" / "ccfleet"]
    data = sysconfig.get_path("data")
    if isinstance(data, str) and data and Path(data).is_absolute():
        roots.append(Path(data).resolve() / "share" / "ccfleet")
    return tuple(dict.fromkeys(roots))


def _directory(path, *, dir_fd=None) -> int:
    flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    return os.open(path, flags, dir_fd=dir_fd)


def _read_document(root: Path, name: str) -> bytes:
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("invalid library root")
    directory = _directory(root.anchor)
    descriptor = None
    try:
        # Walk the fixed base too: no ancestor or document-tree symlink may
        # turn an allowlisted filename into a read from another filesystem path.
        for component in (*root.parts[1:], *name.split("/")[:-1]):
            child = _directory(component, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(name.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o022
                or not 0 < info.st_size <= MAX_DOCUMENT_BYTES):
            raise ValueError("unsafe or oversized library document")
        data = bytearray()
        while len(data) <= MAX_DOCUMENT_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_DOCUMENT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if not data or len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("library document changed beyond its size bound")
        return bytes(data)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def load_document(name: str) -> Optional[bytes]:
    """Load one public asset from fixed installation roots; expose no OS errors."""
    if name not in PUBLIC_FILES:
        return None
    for root in candidate_roots():
        try:
            return _read_document(root, name)
        except (OSError, ValueError):
            continue
    return None
