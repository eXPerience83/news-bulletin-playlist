"""Safe build metadata exposed to status and diagnostic support surfaces."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_BUILD_REVISION_ENV = "NEWS_PLAYLIST_BUILD_REVISION"
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def build_revision(environ: Mapping[str, str] | None = None) -> str:
    """Return a short source revision or ``dev`` for local/invalid builds."""
    env = os.environ if environ is None else environ
    raw = env.get(_BUILD_REVISION_ENV, "").strip().lower()
    if _FULL_GIT_SHA.fullmatch(raw):
        return raw[:12]
    return "dev"
