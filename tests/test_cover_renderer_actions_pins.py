"""Supply-chain guard for the privileged playlist cover renderer."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github/workflows/render-playlist-covers.yml"


def test_cover_render_uses_immutable_actions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow

    references = [
        line.split("uses: ", 1)[1].split()[0]
        for line in workflow.splitlines()
        if line.lstrip().startswith(("uses: ", "- uses: "))
    ]
    assert references
    for reference in references:
        _, separator, sha = reference.rpartition("@")
        assert separator and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
