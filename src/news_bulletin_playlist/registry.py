from __future__ import annotations

from dataclasses import dataclass

from news_bulletin_playlist.providers.base import TitleParser
from news_bulletin_playlist.providers.cnn import CnnParser
from news_bulletin_playlist.providers.ondacero import OndaCeroParser
from news_bulletin_playlist.providers.rne import RneParser
from news_bulletin_playlist.providers.ser import SerParser


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    feed_url: str
    spotify_show_id: str
    parser: TitleParser
    contract_fallback_url: str | None = None


CORE_PROVIDERS: tuple[ProviderConfig, ...] = (
    ProviderConfig(
        provider_id="ser",
        feed_url="https://fapi-top.prisasd.com/podcast/playser/boletines.xml",
        spotify_show_id="4EwwdoHHYmbt49UXODQMpi",
        parser=SerParser(),
    ),
    ProviderConfig(
        provider_id="rne",
        feed_url="https://api.rtve.es/api/adapter/programas/1750/audios.rss",
        spotify_show_id="0UgidTKsoaHiHDARuPQNW1",
        parser=RneParser(),
    ),
    ProviderConfig(
        provider_id="ondacero",
        feed_url=(
            "https://www.ondacero.es/rss/podcast/mount/"
            "ATRESMEDIA_LAS_NOTICIAS_EN_ONDA_CERO_P/fastly"
        ),
        spotify_show_id="0tjEexypyczHXW9vE3SU3P",
        parser=OndaCeroParser(),
        contract_fallback_url="https://www.ondacero.es/podcast/programas/boletines/",
    ),
    ProviderConfig(
        provider_id="cnn",
        feed_url="https://feeds.megaphone.fm/WMHY5696831164",
        spotify_show_id="0vDgnorbpBr65YZzFVVouE",
        parser=CnnParser(),
    ),
)
