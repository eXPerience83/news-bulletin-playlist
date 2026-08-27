# P0 source and Spotify probe findings

Status: public-source research complete as of 2026-08-27. Authenticated Spotify API probing is still required for the remaining Spotify-side questions.

## Product invariants

- Playlist window: last **48 hours**.
- Playlist hard limit: **100 episodes**.
- Sort: `published_at` descending.
- Local metadata retention: **30 days**.
- No podcast audio download or storage.
- Reconcile the whole playlist in one replace operation whenever desired state changes.

## Providers

### Cadena SER

- Provider ID: `ser`
- RSS: `https://fapi-top.prisasd.com/podcast/playser/boletines.xml`
- Spotify show: `4EwwdoHHYmbt49UXODQMpi`
- Known title shape: `Las noticias de la SER, HH:MM (DD/MM/YYYY)`.
- Minutes are not guaranteed to be `00` (observed examples include `23:03` and `19:22`).

### RNE

- Provider ID: `rne`
- Spotify show: `0UgidTKsoaHiHDARuPQNW1`
- RSS variants observed:
  - `https://api.rtve.es/api/adapter/programas/1750/audios.rss`
  - `https://api.rtve.es/api/programas/1750/audios.rss`
- Known title time variants include `18.30`, `18,30`, `1930`, and `19H`.
- Two distinct RTVE assets were published for the 2026-08-25 18:00 edition. Preserve both source identities until Spotify-side behaviour is verified.

### Onda Cero

- Provider ID: `ondacero`
- RSS: `https://www.ondacero.es/rss/podcast/mount/ATRESMEDIA_LAS_NOTICIAS_EN_ONDA_CERO_P/fastly`
- Spotify show: `0tjEexypyczHXW9vE3SU3P`
- Known title shape: `Las noticias de Onda Cero de las H:MMh (D/M/YYYY)`.
- Leading zero is optional for the hour.

### CNN 5 Cosas

- Provider ID: `cnn`
- RSS: `https://feeds.megaphone.fm/WMHY5696831164`
- Spotify show: `0vDgnorbpBr65YZzFVVouE`
- Known title shapes include `MM/DD/YYYY 6 pm` and `MM/DD/YY 6pm`.
- Edition timezone: `America/New_York`.
- Do not use duration as an exact match key; distribution surfaces may round or report it differently.

### COPE

- Provider ID: `cope`
- Public COPE audio feed documented as `https://www.cope.es/api/es/audios/rss.xml`; exact dedicated national show feed remains unresolved.
- Known national title shape: `H:MMH | D MON YYYY | BOLETÍN`.
- Regional/local bulletin titles must be rejected when consuming a broad feed.
- Apple Podcasts, iVoox and COPE distribution are confirmed.
- A current direct Spotify show/episode URL has not been verified publicly. Authenticated Spotify search is required before the provider can be enabled.

## Matching policy candidate

1. Parse each provider title into canonical `edition_at`.
2. Use source `pubDate` as `published_at`.
3. Query the known Spotify show, not global search, whenever the show ID is known.
4. Strong match: normalized title + compatible release calendar date.
5. Duration is a secondary diagnostic only, never an exact key.
6. More than one surviving Spotify candidate => `AMBIGUOUS`; never guess.
7. No candidate => `PENDING`; retry for a grace period before raising provider health incidents.
8. Cache `(provider_id, feed_guid) -> spotify_episode_uri` for the 30-day metadata window.

## Authenticated Spotify probe still required

- Confirm minimum working scopes in current Development Mode.
- Confirm latest 24-48h mappings for SER, RNE, Onda Cero and CNN using market `ES`.
- Check whether Spotify contains one or both RNE 2026-08-25 18:00 republications.
- Search Spotify for `Boletines COPE` and recent exact national bulletin titles in market `ES`.
- Create a temporary private playlist and verify ordered episode replacement + readback.
- Verify repeat reconciliation produces zero writes when desired state is unchanged.

## Branding

Spotify developer branding guidance says the registered application name should not contain `Spotify`. Use a neutral product name and describe it as being "for Spotify" where appropriate.
