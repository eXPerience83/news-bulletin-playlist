"""Shared server-rendered temporal capability selector."""

from news_bulletin_playlist.models import TemporalStrategy

TEMPORAL_STRATEGY_LABELS = {
    TemporalStrategy.AUTO: "Auto only (recommended)",
    TemporalStrategy.FULL_DATETIME: "Auto + full date/editorial clock fallback",
    TemporalStrategy.CLOCK_CATALOGUE_DAY: "Auto + editorial clock/catalogue day fallback",
    TemporalStrategy.PARTIAL_DATE_CATALOGUE_DAY: "Auto + partial date/clock/catalogue day fallback",
    TemporalStrategy.EXPLICIT_UTC: "Auto + explicit UTC/GMT fallback",
    TemporalStrategy.DISABLED: "Disable semantic title detection",
}


def temporal_strategy_selector(selected: TemporalStrategy = TemporalStrategy.AUTO) -> str:
    options = "".join(
        f'<option value="{strategy.value}"'
        f"{' selected' if strategy is selected else ''}>{label}</option>"
        for strategy, label in TEMPORAL_STRATEGY_LABELS.items()
    )
    return (
        f'<label>Title time detection <select name="temporal_strategy">{options}</select></label>'
        "<p>Title time detection and Spotify public release time are independent policies. "
        "Proven title time always takes priority.</p>"
    )
