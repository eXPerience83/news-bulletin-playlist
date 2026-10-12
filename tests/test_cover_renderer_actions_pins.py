"""Supply-chain guard for the privileged playlist cover renderer."""

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/render-playlist-covers.yml"


def test_cover_render_uses_immutable_actions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow

    references = re.findall(r"^\s*- uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert references
    for reference in references:
        assert re.search(r"@[a-f0-9]{40}$", reference), reference
