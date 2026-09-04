# News Bulletin Playlist

Open-source service for dynamic news bulletin playlists across countries and languages,
with Spotify as the first destination.

> Pre-release. The P1 production multi-playlist engine is complete; the current release step is validating the managed multi-playlist catalog in production. This project is not affiliated with or endorsed by Spotify or any news provider.

## Goal

Run **one engine that can maintain multiple news-bulletin playlists** from shared provider data.

Spanish-language news is the first implementation and validation target. The architecture is intentionally multi-playlist, multi-country and multi-language so the same runtime can power English, French, German, Polish and additional playlists without duplicating the application.

Core architectural invariant:

> **Fetch once -> normalize once -> store once -> distribute to many playlists.**

A provider/source is independent from a playlist and may feed several playlists. A single engine run fetches each required source once, normalizes and persists it once, then evaluates and reconciles every configured destination playlist independently.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the architectural contract and invariants.

Initial product defaults:

- keep episodes published within the last **48 hours**;
- cap a playlist at **100 episodes**;
- order by source publication timestamp (`published_at`), newest first;
- make the maximum episode duration a **per-managed-playlist setting** editable from `/admin/`;
- use **30 minutes / 1800 seconds as the current general default for every playlist**;
- prefer bulletin sources whose normal publishing format is concise; a source that is predominantly long-form is not promoted merely because individual episodes might fit under 30 minutes;
- retain operational metadata locally for **30 days**;
- run the production engine approximately every **10 minutes**;
- never download or store podcast audio;
- treat RSS/provider metadata as the timing source and Spotify as the playlist destination;
- allow playlist-specific policies to override defaults when required.

The 30-minute ceiling is deliberately conservative while the catalog is being observed in production. Sanitized diagnostics record accepted episodes of at least 20 minutes and duration exclusions. That evidence will determine whether the product later needs a tighter concise-news ceiling, a separate long-edition playlist, or both. Occasional long editions from an otherwise concise bulletin source are acceptable; sources whose usual product is long-form are not.

## Editorial independence

Playlist inclusion and ordering are based on source/editorial rules, freshness and playlist policy — **never on payment, donations, sponsorship or other compensation**.

Paid playlist placement, guaranteed inclusion, paid priority and sponsor-funded changes to playlist content are explicitly outside the product design.

Possible future monetization of the software or a separate qualifying non-streaming service is a different concern and must remain separate from playlist composition.

## Spotify access model

As of 2026-08-27, Spotify Development Mode is intended for learning, experimentation and personal **non-commercial** projects. The current deployment is therefore treated as non-commercial.

Spotify's Developer Policy permits certain limited commercial uses for qualifying non-streaming applications, but any future commercial use would require re-checking Spotify's then-current access mode, eligibility requirements and policies. Development Mode must not be used as the assumed foundation for building or scaling a business.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the current Spotify platform constraints and the authenticated probes still required.

## Initial providers

The runtime catalog is intentionally stricter than the research list: a source needs a stable feed, a deterministic Spotify-show identity/matching contract and a bulletin-like publishing format suitable for the playlist.

Source research uses a compact **`SCOPE · LANGUAGE`** label so national, international and language/locale fit stay visible independently. Examples include `INT · ES`, `ES · ES`, `INT · FR`, `FR · FR` and `MX · LA`. See [`docs/SOURCE_SELECTION.md`](docs/SOURCE_SELECTION.md) for the classification and promotion rules.

| Provider | Primary fit | Duration profile | Typical format |
| --- | --- | --- | --- |
| Cadena SER | `ES · ES` | Mixed | hourly bulletins, with a longer morning edition |
| RNE | `ES · ES` | Concise | recurring radio bulletins |
| Onda Cero | `ES · ES` | Concise | recurring radio bulletins |
| ABC — Las Noticias de ABC | `ES · ES` | Concise | concise daily news editions |
| CNN 5 Cosas | `INT · ES` | Concise | concise international briefing |
| UN News Today | `INT · EN` | Concise | daily concise global bulletin |
| RFI — Journal Monde | `INT · FR` | Mixed | mostly 10-minute world bulletins; longer tranches are duration-filtered |
| Deutschlandfunk — Die Nachrichten | `INT · DE` | Concise | recurring ~5-minute news bulletins |
| RMF FM — Fakty | `INT · PL` | Mixed | recurring short news bulletins, with occasional longer editions |

Regional Spanish products such as **RFI Español — Noticias de América** remain research candidates rather than default runtime sources because the Spanish playlists are intended to cover Spain plus genuinely global/international news, not one non-Spanish geographic region. Predominantly long-form products such as **BBC Global News Podcast** are likewise not promoted to the runtime catalog under the current concise-source rule.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md), [`docs/SOURCE_SELECTION.md`](docs/SOURCE_SELECTION.md) and issue #53 for continuing source research.

## Container runtime

The supported runtime is a hardened Python 3.14 container. One long-lived process owns the HTTP status/admin surface and one sequential engine scheduler; it does **not** create one cron, worker or container per playlist.

The container uses a read-only root filesystem, runs as a non-root numeric UID/GID, keeps `/data` as its only persistent writable path and drops Linux capabilities. `/healthz` validates persistent storage. The read-only `/` page reports runtime/Spotify state plus last cycle, next run, source outcomes and playlist outcomes when the engine is configured.

For local Docker:

```bash
docker compose up --build -d
docker compose ps
```

The local status page is available only on `http://127.0.0.1:8788/`.

The base container remains healthy with no active managed playlist and reports the engine as **Not configured**. The normal production path does **not** require copying or editing an engine YAML file: configure the protected `/admin/` surface, connect Spotify, review the built-in playlist templates, edit metadata/cover/source selection and maximum episode duration if needed, and activate the desired playlists. The application creates each private Spotify destination, persists installation-owned choices in `/data/managed-state.json`, and wakes the scheduler immediately. See [`docs/DEPLOY_TRUENAS.md`](docs/DEPLOY_TRUENAS.md) for the production PKCE/HTTPS and first-run flow.

For TrueNAS 26-BETA.3 or newer, create a dedicated dataset with the **Apps** preset and install [`deploy/truenas.yaml`](deploy/truenas.yaml) as a **Custom App via YAML**, not as a Community catalog app. The base YAML publishes the read-only status UI on port `8788` and contains no Spotify credentials.

Production `/admin/` and the Spotify callback are intentionally fail-closed. When an external HTTPS origin is configured, administration is accepted only from the explicitly configured immediate reverse-proxy IP/CIDR and only when that proxy asserts `X-Forwarded-Proto: https`. Direct HTTP access to the backend never reaches the Basic-authentication challenge in that mode. Loopback HTTP remains available only for local/P0 development.

## Development

The project targets **Python 3.14**. PyYAML is the only runtime dependency; development checks use pytest, Ruff and mypy.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
mypy src
```

Before creating or updating a PR, developers and Codex can apply all Ruff lint fixes and formatting with one cross-platform command from the repository root:

```bash
python scripts/ruff_fix.py
```

That helper runs `ruff check --fix .` followed by `ruff format .`; Ruff remains the single source of truth and no additional formatting tool or pre-commit dependency is required.

CI runs `ruff check .`, `ruff format --check .`, mypy and pytest on Python 3.14. A separate container workflow validates Docker/Compose changes and publishes successful `main` builds to GHCR.

## Domain and configuration contract

Normal operation separates immutable application knowledge from installation-owned choices. The image ships a built-in catalog of supported sources and managed playlist templates; `/data/managed-state.json` stores only the playlists activated by this installation, their Spotify destination IDs, enabled/paused state, metadata/cover choices, explicit many-to-many source membership and per-playlist duration ceiling. Updating the image can therefore add supported sources/templates without overwriting existing local selections.

The managed state is compiled with the built-in catalog into the same `EngineConfig` used by the production engine. Ordinary administration happens through `/admin/`: built-in source definitions can be assigned or unassigned from playlists but are not deleted from the catalog, and a source selected by several active playlists is still fetched only once per engine cycle. Duration-only changes are local playlist policy and do not require a Spotify reconnect or metadata write.

The schema-v1 full-YAML loader remains an **advanced/manual compatibility path**, not the default first-run workflow. [`config/news-bulletin-playlist.example.yaml`](config/news-bulletin-playlist.example.yaml) is retained for that compatibility path. An explicit `NEWS_PLAYLIST_CONFIG` selects a legacy YAML file; otherwise the runtime prefers `/data/managed-state.json` when present and only falls back to the default legacy `/data/news-bulletin-playlist.yaml`. If managed state and that default legacy YAML both exist, startup fails closed rather than guessing which configuration should win.

The current built-in runtime source IDs are `ser`, `rne`, `ondacero`, `abc`, `cnn`, `un_news_en`, `rfi_fr`, `dlf_news` and `rmf_fakty`. Research candidates remain outside ordinary selectable production configuration until their feed, Spotify identity, matching contract, geographic/editorial scope and typical episode duration have been checked.

Canonical editions are identified only by `(source_id, source_native_id)`. Titles and timestamps are metadata, never identity. Canonical timestamps are timezone-aware UTC values. Spotify show references are source catalogue metadata and are intentionally distinct from writable playlist destinations.

A cycle uses durable canonical/match state when building desired playlists. Therefore a transient source failure does not erase still-valid recent episodes; they age out naturally at the playlist retention boundary. Destination failures are isolated so one Spotify playlist cannot block another. If a source or Spotify catalogue lookup fails before enough last-known-good state exists to establish a safe desired state, the affected destination is preserved with zero reconciliation writes rather than treating an empty desired state as authoritative.

Spotify playlist descriptions are plain text from the Web API contract. Managed descriptions stay attribution-free locally; when metadata is created or explicitly synchronized, the runtime appends one bounded project line containing `https://github.com/eXPerience83/news-bulletin-playlist`. Whether a Spotify client renders that URL as clickable is client behavior and is not assumed by the application.

## License

Released under the [MIT License](LICENSE).

## Security

Do not commit Spotify client secrets, refresh tokens, `.env` files, administration passwords or the runtime database. Production Spotify authorization uses Authorization Code + PKCE and does not require a Spotify Client Secret. The durable refresh credential is stored owner-only under `/data`; access tokens remain memory-only.

The production administration surface must sit behind HTTPS. Configure `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` with the immediate reverse proxy's source IP/CIDR, not a client or Tailscale subnet, and configure the proxy to overwrite `X-Forwarded-Proto` with `https`. Never expose the backend administration port as a trusted alternative to the HTTPS origin. Runtime OAuth operations and scheduler token refreshes are serialized so reconnect/refresh cannot race against the same credential store.

## Roadmap

The completed production-engine roadmap is recorded in the [P1 umbrella issue #13](https://github.com/eXPerience83/news-bulletin-playlist/issues/13).

1. **P0 — validated foundation** — provider contracts and watchdog, hardened container/TrueNAS runtime, plus Spotify catalogue/write probes.
2. **P1 — production multi-playlist engine — complete**:
   - [x] **P1.1 / #14** — source, canonical edition and playlist configuration/domain model; completed via #21.
   - [x] **P1.2 / #15** — shared RSS collection and canonical normalization; completed via #22.
   - [x] **P1.3 / #16** — SQLite persistence, migrations and 30-day operational retention; completed via #26.
   - [x] **P1.4 / #17** — deterministic source-to-Spotify episode matching; completed via #27.
   - [x] **P1.5 / #18** — desired-state generation and multi-playlist Spotify reconciliation; completed via #29.
   - [x] **P1.6 / #19** — production Spotify OAuth callback/token lifecycle through the private Web UI; completed via #30.
   - [x] **P1.7 / #20** — integrated engine cycle, scheduler and operational status in the durable runtime; completed via #31.
3. **Managed catalog validation — current** — operate Spanish plus initial EN/FR/DE/PL playlists from the same TrueNAS instance, export sanitized diagnostics and adjust source/policy choices from real operation.
4. **Expansion** — add more concise, deterministic sources and playlist definitions without duplicating the engine.

Parallel/non-blocking product work such as the playlist cover-art system in #12 may land when its configuration hook is stable, but it must never block bulletin synchronization.
