from pathlib import Path

config = Path("src/news_bulletin_playlist/config.py")
text = config.read_text(encoding="utf-8")
old = 'ordering_raw = data.get("ordering", OrderingPolicy.PUBLISHED_AT_DESC.value)'
new = 'ordering_raw = data.get("ordering", OrderingPolicy.EDITION_AT_DESC.value)'
if old not in text:
    raise SystemExit("config default fragment not found")
config.write_text(text.replace(old, new), encoding="utf-8")

architecture = Path("docs/ARCHITECTURE.md")
text = architecture.read_text(encoding="utf-8")
if "ordering: published_at_desc" not in text:
    raise SystemExit("architecture ordering example not found")
text = text.replace("ordering: published_at_desc", "ordering: edition_at_desc")
old = (
    "The default playlist policy is 48 retention hours, 100 episodes and descending source "
    "publication\ntime."
)
new = (
    "The default playlist policy is 48 retention hours, 100 episodes and descending semantic "
    "bulletin\ntime (`edition_at`), falling back to RSS `published_at` only when no reliable "
    "edition timestamp exists. `published_at_desc` remains available as an explicit legacy "
    "ordering policy."
)
if old not in text:
    raise SystemExit("architecture default-policy paragraph not found")
architecture.write_text(text.replace(old, new), encoding="utf-8")

tests = Path("tests/test_config.py")
text = tests.read_text(encoding="utf-8")
old = "assert playlist.ordering is OrderingPolicy.PUBLISHED_AT_DESC"
new = "assert playlist.ordering is OrderingPolicy.EDITION_AT_DESC"
if old not in text:
    raise SystemExit("test default assertion not found")
tests.write_text(text.replace(old, new, 1), encoding="utf-8")
