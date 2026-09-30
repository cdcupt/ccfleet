"""Allowlisted HTML assets in source and installed layouts, never arbitrary files."""

from __future__ import annotations

import http.client
import json
import os
import threading
from dataclasses import replace

import pytest

from ccfleetd import customer_docs, reference_docs
from ccfleetd.api import Context, build_server
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store


@pytest.fixture
def assets(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "public"
    root.mkdir()
    for name in reference_docs.PUBLIC_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"<!doctype html><html><body>" + name.encode() + b"</body></html>")
        path.chmod(0o644)
    monkeypatch.setattr(reference_docs, "candidate_roots", lambda: (root,))
    return root


@pytest.mark.parametrize("name", sorted(reference_docs.PUBLIC_FILES))
def test_every_canonical_document_is_loaded_without_rewriting_relative_links(assets, name):
    assert reference_docs.load_document(name) == (assets / name).read_bytes()
    assert reference_docs.relative_path(reference_docs.ROUTE_PREFIX + name) == name


def test_tree_relative_links_are_preserved_exactly(assets):
    source = (b'<!doctype html><a href="../README.html">Overview</a>'
              b'<a href="../gateway/README.html">Gateway</a>'
              b'<a href="local-relay.html#privacy">Local</a>')
    (assets / "docs/index.html").write_bytes(source)
    assert reference_docs.load_document("docs/index.html") == source


@pytest.mark.parametrize("name", [
    "../README.html", "docs/../README.html", "/README.html", "docs//design.html",
    "docs/./design.html", "docs/%2e%2e/README.html", "docs%2fdesign.html",
    "docs\\design.html", "docs/design.html/", "docs/design.html\0", "docs/design.md",
    "ccfleetd/api.py", "pyproject.toml", ".env", "docs/private.html",
])
def test_noncanonical_or_unlisted_paths_never_touch_the_filesystem(monkeypatch, name):
    monkeypatch.setattr(reference_docs, "candidate_roots", lambda: pytest.fail("unsafe file lookup"))
    assert reference_docs.relative_path(reference_docs.ROUTE_PREFIX + name) is None
    assert reference_docs.load_document(name) is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "world-writable"])
def test_special_linked_or_writable_documents_are_not_served(assets, tmp_path, kind):
    target = assets / "README.html"
    target.unlink()
    outside = tmp_path / "private-data"
    outside.write_text("PRIVATE MUST NOT BE SERVED")
    outside.chmod(0o600)
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "directory":
        target.mkdir()
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text("not a public file")
        target.chmod(0o666)
    assert reference_docs.load_document("README.html") is None
    assert outside.read_text() == "PRIVATE MUST NOT BE SERVED"


def test_symlinked_document_tree_is_not_followed(assets, tmp_path):
    external = tmp_path / "other"
    external.mkdir()
    (external / "README.html").write_text("PRIVATE")
    (assets / "gateway/README.html").unlink()
    (assets / "gateway").rmdir()
    (assets / "gateway").symlink_to(external, target_is_directory=True)
    assert reference_docs.load_document("gateway/README.html") is None


def test_symlinked_root_ancestor_is_not_followed(assets, tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.symlink_to(assets, target_is_directory=True)
    monkeypatch.setattr(reference_docs, "candidate_roots", lambda: (alias,))
    assert reference_docs.load_document("README.html") is None


def test_missing_empty_oversized_and_growing_files_fail_closed(assets, monkeypatch):
    path = assets / "README.html"
    path.unlink()
    assert reference_docs.load_document("README.html") is None
    path.write_bytes(b"")
    assert reference_docs.load_document("README.html") is None
    monkeypatch.setattr(reference_docs, "MAX_DOCUMENT_BYTES", 20)
    path.write_bytes(b"x" * 21)
    assert reference_docs.load_document("README.html") is None
    path.write_bytes(b"x")
    original = os.read

    def grow(fd, size):
        path.write_bytes(b"x" * 21)
        return original(fd, size)

    monkeypatch.setattr(reference_docs.os, "read", grow)
    assert reference_docs.load_document("README.html") is None


def test_candidate_roots_are_fixed_and_do_not_use_cwd_or_environment(tmp_path, monkeypatch):
    package_parent = tmp_path / "target"
    monkeypatch.setattr(reference_docs, "__file__", str(package_parent / "ccfleetd/reference_docs.py"))
    monkeypatch.setattr(reference_docs.sysconfig, "get_path", lambda name: str(tmp_path / "prefix"))
    monkeypatch.setenv("CCFLEET_DOCS_DIR", str(tmp_path / "untrusted"))
    monkeypatch.chdir(tmp_path)
    assert reference_docs.candidate_roots() == (
        package_parent, package_parent / "share/ccfleet", tmp_path / "prefix/share/ccfleet")


@pytest.mark.parametrize("layout", ["checkout", "target", "sysconfig"])
def test_source_and_wheel_data_file_layouts_are_supported(tmp_path, monkeypatch, layout):
    package_parent = tmp_path / "target"
    data = tmp_path / "prefix"
    monkeypatch.setattr(reference_docs, "__file__", str(package_parent / "ccfleetd/reference_docs.py"))
    monkeypatch.setattr(reference_docs.sysconfig, "get_path", lambda name: str(data))
    root = {"checkout": package_parent, "target": package_parent / "share/ccfleet",
            "sysconfig": data / "share/ccfleet"}[layout]
    destination = root / "docs/index.html"
    destination.parent.mkdir(parents=True)
    destination.write_text("<!doctype html><title>Correct layout</title>")
    assert reference_docs.load_document("docs/index.html") == destination.read_bytes()


@pytest.mark.parametrize("anchor", ["module", "sysconfig"])
def test_trusted_runtime_prefix_alias_is_canonical_but_document_tree_is_not(tmp_path, monkeypatch,
                                                                        anchor):
    actual = tmp_path / "runtime"
    actual.mkdir()
    alias = tmp_path / "runtime-alias"
    alias.symlink_to(actual, target_is_directory=True)
    if anchor == "module":
        monkeypatch.setattr(reference_docs, "__file__", str(alias / "ccfleetd/reference_docs.py"))
        monkeypatch.setattr(reference_docs.sysconfig, "get_path", lambda name: str(tmp_path / "missing"))
    else:
        monkeypatch.setattr(reference_docs, "__file__", str(tmp_path / "missing/ccfleetd/reference_docs.py"))
        monkeypatch.setattr(reference_docs.sysconfig, "get_path", lambda name: str(alias))
    root = actual / "share/ccfleet"
    root.mkdir(parents=True)
    (root / "README.html").write_text("<!doctype html><title>Installed</title>")
    assert reference_docs.load_document("README.html") == (root / "README.html").read_bytes()
    (root / "README.html").unlink()
    outside = tmp_path / "not-public"
    outside.write_text("PRIVATE")
    (root / "README.html").symlink_to(outside)
    assert reference_docs.load_document("README.html") is None


@pytest.fixture(params=[False, True], ids=["product", "broker-only"])
def library_server(cfg, request, assets):
    configured = replace(cfg, admin_host="admin.example.com", public_url="https://fleet.example.com",
                         broker_only=request.param)
    store = Store(":memory:")
    server = build_server(Context(store, configured, Monitor(store, configured, LogNotifier())),
                          host="127.0.0.1", port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server, request.param
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
        store.close()


def request(server, path, *, host="fleet.example.com", method="GET"):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        connection.request(method, path, headers={"Host": host})
        response = connection.getresponse()
        return response.status, response.version, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize("path", ["/docs/library", "/docs/library/"])
def test_library_entry_redirects_to_tree_preserving_index_only_on_product(library_server, path):
    server, broker_only = library_server
    status, version, headers, body = request(server, path)
    if broker_only:
        assert status == 404 and "Location" not in headers
    else:
        assert (status, version, body) == (303, 10, b"")
        assert headers["Location"] == "/docs/library/docs/index.html"


@pytest.mark.parametrize("name", sorted(reference_docs.PUBLIC_FILES))
def test_all_public_library_routes_have_html_type_and_unchanged_body(library_server, assets, name):
    server, broker_only = library_server
    status, version, headers, body = request(server, reference_docs.ROUTE_PREFIX + name)
    assert version == 10
    if broker_only:
        assert status == 404
    else:
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert body == (assets / name).read_bytes()


@pytest.mark.parametrize("host", ["admin.example.com", "127.0.0.1", "localhost"])
def test_admin_site_never_reads_or_serves_library_documents(library_server, monkeypatch, host):
    server, _ = library_server
    monkeypatch.setattr(reference_docs, "load_document", lambda name: pytest.fail("admin file read"))
    assert request(server, "/docs/library", host=host)[0] == 404
    assert request(server, reference_docs.INDEX_PATH, host=host)[0] == 404


def test_unknown_and_encoded_library_paths_are_404_without_path_disclosure(library_server):
    server, _ = library_server
    for path in ("/docs/library/docs/missing.html", "/docs/library/../pyproject.toml",
                 "/docs/library/docs%2findex.html", "/docs/library/docs/%2e%2e/README.html"):
        status, _, _, body = request(server, path)
        assert status == 404 and json.loads(body) == {"error": "not found"}


def test_library_head_returns_length_without_body(library_server, assets):
    server, broker_only = library_server
    status, _, headers, body = request(server, reference_docs.INDEX_PATH, method="HEAD")
    assert body == b""
    if broker_only:
        assert status == 404
    else:
        assert status == 200 and int(headers["Content-Length"]) == len((assets / "docs/index.html").read_bytes())


def test_customer_pages_link_to_the_public_html_library(cfg):
    for page in (customer_docs.overview, customer_docs.guide, customer_docs.how_it_works,
                 customer_docs.terms):
        assert '<a href="/docs/library">HTML reference library</a>' in page(cfg)
