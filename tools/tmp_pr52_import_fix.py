from pathlib import Path

path = Path("tests/test_managed_admin_runtime.py")
text = path.read_text()
old = "from news_bulletin_playlist.catalog import BUILTIN_CATALOG\nfrom news_bulletin_playlist.engine import EngineCycleResult, OperationalStatus\nfrom news_bulletin_playlist.effective_config import CONFIG_PATH_ENV\n"
new = "from news_bulletin_playlist.catalog import BUILTIN_CATALOG\nfrom news_bulletin_playlist.effective_config import CONFIG_PATH_ENV\nfrom news_bulletin_playlist.engine import EngineCycleResult, OperationalStatus\n"
if text.count(old) != 1:
    raise SystemExit("expected import block exactly once")
path.write_text(text.replace(old, new))
