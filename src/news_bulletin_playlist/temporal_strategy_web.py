"""Shared server-rendered temporal capability selector."""

from news_bulletin_playlist.models import TemporalStrategy

TEMPORAL_STRATEGY_LABELS = {
    TemporalStrategy.AUTO: "Auto (recommended)",
    TemporalStrategy.FULL_DATETIME: "Full date + editorial clock",
    TemporalStrategy.CLOCK_CATALOGUE_DAY: "Editorial clock + Spotify catalogue day",
    TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY: "Partial date + clock + Spotify catalogue day",
    TemporalStrategy.EXPLICIT_UTC: "Full date + explicit UTC/GMT clock",
    TemporalStrategy.DISABLED: "Disabled / fallback only",
}


def temporal_strategy_selector(selected: TemporalStrategy = TemporalStrategy.AUTO) -> str:
    options = "".join(
        f'<option value="{strategy.value}"'
        f"{' selected' if strategy is selected else ''}>{label}</option>"
        for strategy, label in TEMPORAL_STRATEGY_LABELS.items()
    )
    return f'<label>Temporal detection <select name="temporal_strategy">{options}</select></label>'
