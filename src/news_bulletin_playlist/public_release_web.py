"""Shared source policy selector, independent of semantic title capabilities."""

from news_bulletin_playlist.models import PublicReleaseMode

PUBLIC_RELEASE_MODE_LABELS = {
    PublicReleaseMode.INSTANT: "Instant (recommended)",
    PublicReleaseMode.WALL_TIME: "Wall time in source timezone",
    PublicReleaseMode.DISABLED: "Disabled",
}


def public_release_selector(selected: PublicReleaseMode = PublicReleaseMode.INSTANT) -> str:
    options = "".join(
        f'<option value="{mode.value}"{" selected" if mode is selected else ""}>{label}</option>'
        for mode, label in PUBLIC_RELEASE_MODE_LABELS.items()
    )
    return (
        f'<label>Spotify public release time <select name="public_release_mode">{options}'
        "</select></label>"
        "<p>Instant: Use the timezone/offset encoded by Spotify. "
        "Wall time: Treat Spotify's written date and clock as local time "
        "in this source's timezone. "
        "Disabled: Do not use Spotify public release time for exact chronology.</p>"
    )
