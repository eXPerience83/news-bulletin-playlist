# P0 source and Spotify probe findings

Status: public-source research complete as of 2026-08-27. Authenticated Spotify API probing is still required for the remaining Spotify-side questions.

## Product invariants

- Playlist window: last **48 hours**.
- Playlist hard limit: **100 episodes**.
- Sort: `published_at` descending.
- Local metadata retention: **30 days**.
- No podcast audio download or storage.
- Reconcile the whole playlist in one replace operation whenever desired state changes.
- No payment, donation, sponsorship or other compensation may influence inclusion, ordering, naming or other editorial content of a Spotify playlist.

## Spotify platform constraints (2026-08-27)

The current project is designed as a **non-streaming** integration: Spotify is used as the catalogue/playlist destination and the application does not provide Spotify audio playback.

Current Development Mode constraints documented by Spotify include:

- intended use is learning, experimentation and personal **non-commercial** projects;
- Spotify Premium is required for Development Mode;
- one Development Mode Client ID per developer;
- up to five authorized users per Client ID;
- API access is limited compared with wider partner access.

Spotify also states that Development Mode should not be relied on as a foundation for building or scaling a business.

Extended quota access is not currently a realistic assumption for this project. Spotify's published criteria include application by an established business/organization rather than an individual, an already launched service, at least **250,000 monthly active users**, availability in key markets, commercial viability and policy compliance.

Spotify's Developer Policy allows certain commercial uses for qualifying **Non-Streaming SDAs**, including selling access to the SDA and advertising/sponsorship on the SDA itself. This does **not** make the current Development Mode deployment commercial: any future monetization would require re-checking the then-current access mode, terms and approval requirements.

Paid playlist placement is intentionally excluded from the product. Spotify states that accepting or offering compensation to influence the name or content of a user playlist is not permitted. Therefore sponsorship/donation logic must never affect playlist membership or ordering.

Official references:

- https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- https://developer.spotify.com/policy
- https://artists.spotify.com/en/blog/behind-the-playlists-your-questions-answered-by-our-playlist-editors

The relevant API endpoints and scopes still need to be verified with the authenticated probe because Spotify's Development Mode endpoint restrictions have changed during 2026 and rollout timing has been adjusted.

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
- Confirm which playlist/show/episode endpoints needed by this project remain available to a newly created Development Mode Client ID.
- Confirm latest 24-48h mappings for SER, RNE, Onda Cero and CNN using market `ES`.
- Check whether Spotify contains one or both RNE 2026-08-25 18:00 republications.
- Search Spotify for `Boletines COPE` and recent exact national bulletin titles in market `ES`.
- Create a temporary probe playlist and verify ordered episode replacement + readback.
- Verify repeat reconciliation produces zero writes when desired state is unchanged.

## Branding

Spotify developer branding guidance says the registered application name should not contain `Spotify`. Use a neutral product name and describe it as being "for Spotify" where appropriate.
