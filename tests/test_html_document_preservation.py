"""Canonical HTML docs retain visible guidance, literal commands and working links."""
from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr"}
HIDDEN = {"head", "script", "style", "template"}
BLOCK = {"article", "aside", "blockquote", "br", "div", "dl", "dt", "dd", "figcaption",
         "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
         "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "th", "tr", "ul"}


def normalized(text):
    return " ".join(text.split())


class Document(HTMLParser):
    """Parse rendered semantics, not tag-stripped script/style source strings."""

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.stack = []
        self.parts = []
        self.ids = set()
        self.duplicate_ids = []
        self.links = []
        self.tags = []
        self.meta = []
        self.title = []
        self.styles = []
        self.headings = []
        self.codes = []
        self.pre_codes = []
        self.tables = []
        self.code_elements = []
        self._heading = None
        self._code_buffers = []
        self.doctype = False
        self.feed(source)
        self.close()
        self.text = normalized("".join(self.parts))

    def handle_decl(self, declaration):
        self.doctype = declaration.lower() == "doctype html"

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.tags.append((tag, attributes))
        if attributes.get("id"):
            identifier = attributes["id"]
            if identifier in self.ids:
                self.duplicate_ids.append(identifier)
            self.ids.add(identifier)
        if tag == "a" and attributes.get("href") is not None:
            self.links.append(attributes["href"])
        if tag == "meta":
            self.meta.append(attributes)
        if tag == "table":
            self.tables.append(list(self.stack))
        if self._code_buffers:
            self.code_elements.append((tag, attributes))
        if tag == "code":
            self._code_buffers.append([])
            if any(name.lower().startswith("on") for name in attributes):
                self.code_elements.append((tag, attributes))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = (tag, [])
        if tag in BLOCK:
            self.parts.append("\n")
        if tag not in VOID:
            self.stack.append((tag, attributes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "code" and self._code_buffers:
            code = "".join(self._code_buffers.pop())
            self.codes.append(code)
            if any(name == "pre" for name, _ in self.stack):
                self.pre_codes.append(code)
        if self._heading is not None and self._heading[0] == tag:
            self.headings.append((tag, normalized("".join(self._heading[1]))))
            self._heading = None
        if tag in BLOCK:
            self.parts.append("\n")
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        ancestors = {tag for tag, _ in self.stack}
        if "style" in ancestors:
            self.styles.append(data)
        if "title" in ancestors:
            self.title.append(data)
        if HIDDEN.intersection(ancestors):
            return
        self.parts.append(data)
        if self._heading is not None:
            self._heading[1].append(data)
        for buffer in self._code_buffers:
            buffer.append(data)


def visible_html(source):
    return Document(source).text


def read_document(path):
    return Document(Path(path).read_text(encoding="utf-8"))


def canonical_pages():
    return [ROOT / "README.html", ROOT / "gateway/README.html",
            *sorted((ROOT / "docs").rglob("*.html"))]


def test_visible_parser_ignores_hidden_script_style_and_template_claims():
    source = ("<html><head><title>Hidden title</title><style>.x{content:'unsafe claim'}</style>"
              "</head><body><script>privacy = 'unsafe claim';</script>"
              "<template>unsafe claim</template><p>Visible <code>ccfleet local</code>.</p>"
              "<pre><code>&lt;script&gt;literal, not executable&lt;/script&gt; &amp;&amp; run"
              "</code></pre></body></html>")
    page = Document(source)
    assert "unsafe claim" not in page.text and "Hidden title" not in page.text
    assert "Visible ccfleet local." in page.text
    assert "<script>literal, not executable</script> && run" in page.codes
    assert page.code_elements == []


def test_canonical_document_sources_are_html_not_markdown():
    markdown = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"),
                *(ROOT / "gateway").glob("*.md")]
    assert not markdown, [str(path.relative_to(ROOT)) for path in markdown]
    assert (ROOT / "README.html").is_file()
    assert (ROOT / "gateway/README.html").is_file()
    assert (ROOT / "docs/index.html").is_file()


def test_every_canonical_page_is_standalone_utf8_and_mobile_ready():
    pages = canonical_pages()
    assert len(pages) >= 15
    for path in pages:
        page = read_document(path)
        tags = {name for name, _ in page.tags}
        assert page.doctype, path
        assert {"html", "head", "body", "title", "h1"} <= tags, path
        assert normalized("".join(page.title)), path
        assert any(meta.get("charset", "").lower() == "utf-8" for meta in page.meta), path
        assert any(meta.get("name", "").lower() == "viewport"
                   and "width=device-width" in meta.get("content", "") for meta in page.meta), path
        assert any(name == "html" and attrs.get("lang") for name, attrs in page.tags), path
        assert not page.duplicate_ids, (path, page.duplicate_ids)
        assert "</body>" in page.source.lower() and "</html>" in page.source.lower(), path


def test_all_local_document_links_and_fragments_resolve_inside_the_repository():
    pages = {path.resolve(): read_document(path) for path in canonical_pages()}
    for path, page in pages.items():
        for href in page.links:
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc:
                assert not parsed.scheme or parsed.scheme.lower() in {"http", "https", "mailto"}, href
                continue
            target = ((path.parent / unquote(parsed.path)).resolve() if parsed.path else path)
            assert target.is_relative_to(ROOT.resolve()), (path, href)
            assert target.exists(), (path, href)
            assert target.suffix.lower() != ".md", (path, href)
            if parsed.fragment:
                assert target.suffix.lower() == ".html", (path, href)
                destination = pages.get(target) or read_document(target)
                assert unquote(parsed.fragment) in destination.ids, (path, href)


@pytest.mark.parametrize("relative,commands", [
    ("README.html", [
        "curl -fsSL https://raw.githubusercontent.com/cdcupt/ccfleet/main/laptop/install.sh "
        "| bash -s -- --setup",
    ]),
    ("docs/runbooks.html", [
        "ccfleetd node add <node-id> --owner <name> --region <region> [--rc-expected]",
        "ccfleetd node access <machine> --host <address-reachable-from-bwh> --port 22 "
        "--host-key-file <copied-public-host-key>",
        "echo '/swapfile none swap sw 0 0' >> /etc/fstab",
    ]),
    ("gateway/README.html", [
        'export GW_KEY_A="$(openssl rand -hex 32)"',
        'export ANTHROPIC_CUSTOM_HEADERS="X-Gw-Key: $GW_KEY_A"',
    ]),
])
def test_full_shell_commands_survive_as_literal_escaped_code(relative, commands):
    page = read_document(ROOT / relative)
    code = [normalized(value) for value in page.codes]
    for command in commands:
        assert any(command in value for value in code), (relative, command)


# Captured from all fenced code blocks immediately before the 2026-09-30 HTML
# conversion. Whitespace is normalized, but no command, operator, placeholder,
# quote or diagram character may disappear in a format-only migration.
@pytest.mark.parametrize("relative,count,digest", [
    ("README.html", 13, "f35a13ca10677cce50548f5c0c270ee5e47cbd61870ca89015d4a90d8da6169c"),
    ("gateway/README.html", 2, "05a5875a4103537bf322ea6a96eaf76efd2d1b47bdaa9da8d1ec89921a9feb69"),
    ("docs/compliance.html", 1, "2ded188e10c277d7ba59ba03370ff9b307bcae82bf0704dba331489a5a70208d"),
    ("docs/design.html", 4, "7f419e01e1cc8c555781096ad52e2c6b3cdb122d828a16e0801be29e047efb2b"),
    ("docs/local-relay.html", 11, "a2a25b87109667446ed60fe08276a98b9951e5bdda60bb2e9a61eab375212232"),
    ("docs/project-workspaces.html", 3, "4ffcd619921bc03a4b1a8e660378d00f67b01746ceb7b34ee10553917c4538e0"),
    ("docs/tunnel.html", 8, "d5068c44dd70fc6f7dc50e6ffd9d8abae4f0fb160ab3917aedd732558743e492"),
])
def test_all_pre_conversion_code_blocks_are_preserved(relative, count, digest):
    blocks = [normalized(value) for value in read_document(ROOT / relative).pre_codes]
    assert len(blocks) == count, relative
    actual = hashlib.sha256(json.dumps(blocks, ensure_ascii=False,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    assert actual == digest, relative


def test_code_examples_do_not_become_executable_html_elements():
    for path in canonical_pages():
        page = read_document(path)
        for tag, attributes in page.code_elements:
            assert tag in {"span", "br"}, (path, tag)
            assert not any(name.lower().startswith("on") for name in attributes), (path, attributes)


def scrollable(ancestors, styles):
    overflow = re.compile(r"overflow(?:-x)?\s*:\s*(?:auto|scroll)\b", re.I)
    for tag, attributes in reversed(ancestors):
        if tag not in {"div", "section", "figure"}:
            continue
        if overflow.search(attributes.get("style", "")):
            return True
        for token in attributes.get("class", "").split():
            for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", styles):
                if re.search(r"\." + re.escape(token) + r"(?![\w-])", selector) and overflow.search(
                        declarations):
                    return True
    return False


def test_comparison_tables_have_their_own_horizontal_scroll_wrapper():
    page = read_document(ROOT / "docs/platform-comparison.html")
    assert page.tables
    assert all(scrollable(ancestors, "".join(page.styles)) for ancestors in page.tables)
    for phrase in ("CC Host documents", "CC Fleet", "Assessment", "no blanket superiority",
                   "exact fingerprint/TLS stack and renewal algorithm remain unverified"):
        assert phrase.lower() in page.text.lower()


def test_guidebook_is_conspicuously_historical_and_links_to_the_current_workflow():
    page = read_document(ROOT / "docs/guidebook.html")
    assert "historical" in page.text[:1000].lower()
    assert "current" in page.text[:1000].lower()
    assert any(urlsplit(href).path.endswith("local-relay.html") for href in page.links)
