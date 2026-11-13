"""Illustrated current architecture keeps accessible titles through projection."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ccfleetd import bilingual
from tests.test_html_document_preservation import Document

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name,figures", [
    ("platform-comparison", 6),
    ("devin", 1),
    ("devin-trial-foundation", 3),
])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_current_diagram_titles_ids_and_responsive_variants_survive_projection(name, figures, language):
    source = (ROOT / "docs" / (name + ".html")).read_text()
    target = "/docs/library/docs/" + name + ".html"
    projected = bilingual.project_html(source, language, target)
    page = Document(projected)
    assert not page.duplicate_ids
    authored = {}
    for raw in re.findall(r"<svg\b.*?</svg>", source, re.S):
        svg = ET.fromstring(raw)
        if "arch-svg" in svg.get("class", "").split():
            authored[svg.get("id")] = svg.get("aria-label")
    diagrams = []
    for raw in re.findall(r"<svg\b.*?</svg>", projected, re.S):
        svg = ET.fromstring(raw)
        if "arch-svg" not in svg.get("class", "").split():
            continue
        description = svg.find("{*}desc")
        assert svg.get("role") == "img" and (svg.get("viewBox") or svg.get("viewbox"))
        assert svg.find("{*}title") is None and description is not None
        assert svg.get("aria-label") == authored[svg.get("id")]
        assert svg.get("aria-label").strip()
        assert "".join(description.itertext()).strip()
        assert svg.get("aria-describedby") == description.get("id")
        assert description.get("id") in page.ids
        assert "-" + language + "-" in svg.get("id")
        diagrams.append(svg)
    assert len(diagrams) == figures * 2
    assert sum("arch-mobile" in svg.get("class") for svg in diagrams) == figures
    assert sum("arch-desktop" in svg.get("class") for svg in diagrams) == figures
