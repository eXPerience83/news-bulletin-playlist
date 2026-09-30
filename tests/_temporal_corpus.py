"""Reproducible differential matrix; release days are synthetic, never live API data."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from itertools import product


@dataclass(frozen=True)
class TemporalCase:
    parser: str
    title: str
    day: str
    zone: str


def temporal_matrix() -> list[TemporalCase]:
    cases = []
    for year, month, day, clock in product(
        (2024, 2026, 2068, 2069),
        range(1, 13),
        (1, 13, 28, 31),
        ("00:00", "02:30", "12:00", "23:59", "24:00"),
    ):
        release = date(year, month, min(day, calendar.monthrange(year, month)[1])).isoformat()
        dmy = f"{day:02}/{month:02}/{year}"
        mdy = f"{month:02}/{day:02}/{year}"
        named = (
            "ENE",
            "FEB",
            "MAR",
            "ABR",
            "MAY",
            "JUN",
            "JUL",
            "AGO",
            "SEP",
            "OCT",
            "NOV",
            "DIC",
        )[month - 1]
        values = {
            "ser": f"Las noticias de la SER, {clock} ({dmy})",
            "rne": f"NOTICIAS RNE - {dmy.replace('/', '.')} - {clock.replace(':', '.')}H",
            "ondacero": f"Las noticias de Onda Cero de las {clock}h ({dmy})",
            "cnn": f"CNN 5 cosas {mdy} {clock}am",
            "cope": f"{clock}H | {day} {named} {year} | BOLETÍN",
            "rfi_journal_monde": f"Journal {dmy} {clock.replace(':', 'h')} GMT",
        }
        for parser, title in values.items():
            zone = {"cnn": "America/New_York", "rfi_journal_monde": "UTC"}.get(
                parser, "Europe/Madrid"
            )
            cases.append(TemporalCase(parser, title, release, zone))
    for date_token, clock, prefix_sep, clock_sep, suffix in product(
        ("20.06.2026", "20.06.26", "200626", "20062026", "20.06.2026."),
        ("19", "19.30", "19,30", "1930", "930"),
        (" - ", " -", " "),
        (" - ", "- ", "-", " | "),
        ("", "H", " H", "H."),
    ):
        cases.append(
            TemporalCase(
                "rne",
                f"NOTICIAS RNE{prefix_sep}{date_token}{clock_sep}{clock}{suffix}",
                "2026-06-20",
                "Europe/Madrid",
            )
        )
    return cases
