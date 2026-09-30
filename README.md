# News Bulletin Playlist

Service for dynamic news bulletin playlists across countries and languages,
with Spotify as the first destination.

> Pre-release. The runtime is Spotify-first: public Spotify state is a change signal and the official Spotify Web API show catalogue is the episode authority. This project is not affiliated with or endorsed by Spotify or any news provider.

Version **0.1.0** establishes the first supported persistence compatibility baseline: database
format 1 in `/data/news-bulletin-playlist-v1.sqlite3`. The private-development database at
`/data/news-bulletin-playlist.sqlite3` is not migrated into this baseline. From 0.1.0 forward,
persistence changes require explicit reviewed forward migrations from database format 1.
The current format is 2: startup migrates format 1 in place, backfilling existing accepted
Spotify-first episode rows into a separate exact-duration research history. The filename is unchanged.

## Goal

Run **one engine that can maintain multiple news-bulletin playlists** from shared provider data.

Spanish-language news is the first implementation and validation target. The architecture is intentionally multi-playlist, multi-country and multi-language so the same runtime can power English, French, German, Polish and additional playlists without duplicating the application.

Core architectural invariant:

> **Detect changes -> refresh only what is due -> persist Spotify episodes -> compute locally -> reconcile safely.**

A source is independent from a playlist and may feed several playlists. Public Spotify observation is used only to decide whether a source catalogue needs refreshing. The official Spotify Web API show catalogue supplies episode IDs, URIs and metadata; unchanged sources reuse durable local state, then every configured destination playlist is evaluated and reconciled independently.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the architectural contract and invariants. Notable product/runtime changes are tracked in [`CHANGELOG.md`](CHANGELOG.md).

Initial product defaults:

- keep episodes published within the last **48 hours**;
- cap a playlist at **100 episodes**;
- order by parsed semantic edition timestamp (`edition_at`) when available; otherwise preserve explicit Spotify release-date/first-seen provenance without inventing minute precision;
- make the maximum episode duration a **per-managed-playlist setting** editable from `/admin/`;
- use **30 minutes / 1800 seconds as the current general default for every playlist**;
- prefer bulletin sources whose normal publishing format is concise; a source that is predominantly long-form is not promoted merely because individual episodes might fit under 30 minutes;
- retain operational metadata locally for **30 days**;
- run the production engine approximately every **10 minutes**;
- never download or store podcast audio;
- use Spotify episode ID/URI as canonical runtime identity and the official show catalogue as episode authority;
- allow playlist-specific policies to override defaults when required.

The 30-minute ceiling is deliberately provisional while the catalog is being observed in production. Sanitized diagnostics record duration exclusions, accepted editions of at least 20 minutes and per-source/per-playlist duration histograms. Issue #132 uses that evidence to decide the eventual default hard ceiling. Occasional long editions from an otherwise concise bulletin source are acceptable; sources whose usual product is long-form are not.

## Editorial independence

Playlist inclusion and ordering are based on source/editorial rules, freshness and playlist policy — **never on payment, donations, sponsorship or other compensation**.

Paid playlist placement, guaranteed inclusion, paid priority and sponsor-funded changes to playlist content are explicitly outside the product design.

Possible future monetization of the software or a separate qualifying non-streaming service is a different concern and must remain separate from playlist composition.

## Spotify access model

As of 2026-08-27, Spotify Development Mode is intended for learning, experimentation and personal **non-commercial** projects. The current deployment is therefore treated as non-commercial.

Spotify's Developer Policy permits certain limited commercial uses for qualifying non-streaming applications, but any future commercial use would require re-checking Spotify's then-current access mode, eligibility requirements and policies. Development Mode must not be used as the assumed foundation for building or scaling a business.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for historical Spotify platform research and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the current runtime contract.

## Initial providers

The runtime catalog is intentionally stricter than the research list: a source needs one deterministic Spotify show identity, suitable title/filter rules and a bulletin-like publishing format suitable for the playlist.

Source research and the admin UI classify each product on three independent axes:

> **`ORIGIN · SCOPE · LANGUAGE`**

`ORIGIN` is the provider's country or institution, independently of editorial coverage and language; no country is invented for an institution. `SCOPE` is `LOC`, `REG`, `NAT`, `INT` or `MIX`. **CNN 5 Cosas is `US · INT · es-ES`**, so its US origin does not make it US-local; it is the current default only for the international Spain-Spanish product, not the Spain-national playlist. See [`docs/SOURCE_SELECTION.md`](docs/SOURCE_SELECTION.md).

| Provider | Classification | Duration profile | Typical format |
| --- | --- | --- | --- |
| Cadena SER | `ES · NAT · es-ES` | Mixed | hourly bulletins, with a longer morning edition |
| RNE | `ES · NAT · es-ES` | Concise | recurring radio bulletins |
| Onda Cero | `ES · NAT · es-ES` | Concise | recurring radio bulletins |
| ABC — Las Noticias de ABC | `ES · NAT · es-ES` | Concise | concise daily news editions |
| CNN 5 Cosas | `US · INT · es-ES` | Concise | concise international briefing; default for `INT · es-ES` |
| UN News Today | `UN · INT · en` | Concise | daily concise global bulletin |
| Reuters — World News | `GB · INT · en` | Concise | daily global English briefing; default alongside UN News Today for `INT · EN` |
| United Nations — ONU en minutos | `UN · INT · es` | Concise | filtered global Spanish bulletin; spoken locale still unresolved |
| N+ Univision 24-7 | `US · INT · es` | Concise | daily international/US Spanish newscast; spoken locale still unresolved |
| DW — Actualidad en análisis | `DE · INT · es` | Mixed | international Spanish analysis with occasional >30-minute editions filtered by destination policy |
| RFI — Journal Monde | `FR · INT · fr` | Concise selected product | dedicated parser/filter admits only Journal Monde editions from the mixed Spotify product |
| Deutschlandfunk — Die Nachrichten | `DE · MIX · de` | Concise | recurring short Germany/world bulletins |
| RMF FM — Fakty | `PL · MIX · pl` | Mixed | recurring short Poland/world bulletins, with occasional longer editions |

Regional Spanish products such as **RFI Español — Noticias de América** remain research candidates rather than default runtime sources because Latin-American/Americas-focused news is kept separate from Spain-focused and world-Spanish products. Predominantly long-form products such as **BBC Global News Podcast** are likewise not promoted to the runtime catalog under the current concise-source rule.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md), [`docs/SOURCE_CATALOG.md`](docs/SOURCE_CATALOG.md), [`docs/SOURCE_SELECTION.md`](docs/SOURCE_SELECTION.md) and issue #53 for continuing source research.

## Container runtime

The supported runtime is a hardened Python 3.14 container. One long-lived process owns the HTTP status/admin surface and one sequential engine scheduler; it does **not** create one cron, worker or container per playlist.

The container uses a read-only root filesystem, runs as a non-root numeric UID/GID, keeps `/data` as its only persistent writable path and drops Linux capabilities. `/healthz` validates persistent storage. The read-only `/` page reports runtime/Spotify state plus last cycle, next run, source outcomes and playlist outcomes when the engine is configured.

For local Docker:

```bash
docker compose up --build -d
docker compose ps
```

The local status page is available only on `http://127.0.0.1:8788/`.

The base container remains healthy with no active managed playlist and reports the engine as **Not configured**. The normal production path does **not** require copying or editing an engine YAML file: configure the protected `/admin/` surface, connect Spotify, review the built-in playlist templates, edit metadata/cover/source selection and maximum episode duration if needed, and activate the desired playlists. The application creates each Spotify destination with the current public product semantics, persists installation-owned choices in `/data/managed-state.json`, and wakes the scheduler immediately. See [`docs/DEPLOY_TRUENAS.md`](docs/DEPLOY_TRUENAS.md) for the current trusted-LAN TrueNAS first-run flow and the separately supported hardened HTTPS/reverse-proxy mode.

For TrueNAS 26-BETA.3 or newer, create a dedicated dataset with the **Apps** preset and install [`deploy/truenas.yaml`](deploy/truenas.yaml) as a **Custom App via YAML**, not as a Community catalog app. The base YAML publishes the read-only status UI on port `8788` and contains no Spotify credentials.

Production `/admin/` and the Spotify callback are intentionally fail-closed. When an external HTTPS origin is configured, administration is accepted only from the explicitly configured immediate reverse-proxy IP/CIDR and only when that proxy asserts `X-Forwarded-Proto: https`. Direct HTTP access to the backend cannot reach the first-party `/admin/login` form in that mode. Successful login creates a server-side session with a session cookie. Loopback HTTP remains available only for local/P0 development.

## Development

The project targets **Python 3.14** and currently has no third-party runtime dependencies; development checks use pytest, Ruff and mypy.

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

Normal operation separates immutable application knowledge from installation-owned choices. The image ships a built-in catalog of supported sources and managed playlist templates; schema v4 `/data/managed-state.json` stores activated playlists and canonical installation-owned Spotify-first source definitions. Existing v1/v2 files migrate automatically and atomically to v4 with `sources: []`; v3 upgrades preserve custom sources and default an absent temporal strategy to Auto. Custom source records contain only a canonical Spotify show ID and bounded classification/configuration fields, never OAuth credentials or raw provider responses. Admin Sources now offers an authenticated Add Source flow; built-in definitions remain image-owned, and a built-in/custom identity conflict fails closed rather than overwriting either source.

The Add Source flow accepts only a canonical 22-character show ID, `spotify:show:<id>`, or `https://open.spotify.com/show/<id>` (optionally with one `si` share parameter). It extracts the ID locally, then uses known Spotify API endpoints for bounded validation; it never fetches the pasted URL. Inspection remains transient and Create performs fresh validation before saving only the canonical source definition. Safe metadata editing, reversible enable/disable, playlist assignment, and explicit deletion of unreferenced custom sources are available.

The managed state is compiled with the built-in catalog into the same `EngineConfig` used by the production engine. Ordinary administration happens through `/admin/`: built-in source definitions can be assigned or unassigned from playlists but are not deleted from the catalog. A disabled custom source remains assigned durably but cannot authorize a playlist write; the existing Spotify destination is preserved until the operator later re-enables or explicitly unassigns it. A source selected by several active playlists is refreshed at most once when a detector change, inactivity audit or explicit stale-item escalation makes that catalogue due; unchanged source state is reused locally. Duration-only changes are local playlist policy and do not require a Spotify reconnect or metadata write.

The runtime now has one installation configuration model: the built-in catalog plus versioned `/data/managed-state.json`, administered through `/admin/`. The pre-1.0 full-YAML runtime path has been retired. To avoid silently starting a different configuration, a non-empty obsolete `NEWS_PLAYLIST_CONFIG` or a leftover `/data/news-bulletin-playlist.yaml` causes startup to fail with an actionable removal message; the obsolete file is never read, rewritten or deleted automatically.

The current built-in runtime source IDs are `ser`, `rne`, `ondacero`, `abc`, `cnn`, `un_news_en`, `reuters_world`, `nplus_univision`, `dw_actualidad`, `un_news_es`, `rfi_fr`, `dlf_news` and `rmf_fakty`. Research candidates remain outside ordinary selectable production configuration until their Spotify show identity, catalogue behavior, parser/filter contract, editorial scope and typical episode duration have been checked.

Spotify episode ID/URI is the canonical runtime episode identity. Titles, release-date metadata, parsed `edition_at`, first-seen timestamps and timestamp provenance are metadata, never an alternative identity. Spotify show references identify read-only source catalogues and remain distinct from writable playlist destinations.

A cycle builds desired playlists from durable Spotify-first episode state. Therefore a detector/catalogue failure does not erase still-valid recent episodes; they age out naturally at the playlist retention boundary. Normal detector changes refresh only the changed source. Inactivity audits are configurable and exceptional stale/unavailable-item evidence may refresh other relevant catalogues. Destination failures are isolated so one Spotify playlist cannot block another, and unsafe/ambiguous outcomes preserve the existing destination rather than treating missing evidence as authoritative emptiness.

Spotify playlist descriptions are plain text from the Web API contract. Managed descriptions stay attribution-free locally; when metadata is created or explicitly synchronized, the runtime appends one bounded project line containing `https://github.com/eXPerience83/news-bulletin-playlist`. Whether a Spotify client renders that URL as clickable is client behavior and is not assumed by the application.

## Copyright

Copyright (c) 2026 eXPerience83. All rights reserved.

The current codebase is not offered under an open-source license. No permission is granted to use,
copy, modify, distribute, sublicense or otherwise exploit the current repository state unless
separately authorized in writing by the copyright holder. This does not revoke rights already
granted for historical copies that were distributed under the MIT License.

## Security

Do not commit Spotify client secrets, refresh tokens, `.env` files, administration passwords or the runtime database. Production Spotify authorization uses Authorization Code + PKCE and does not require a Spotify Client Secret. The durable refresh credential is stored owner-only under `/data`; access tokens remain memory-only.

The current TrueNAS production choice is explicit trusted-LAN administration: set `NEWS_PLAYLIST_ADMIN_MODE=lan`, keep backend port `8788` private to the trusted LAN, and do not configure `NEWS_PLAYLIST_EXTERNAL_URL` or `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS`. Operators sign in at `/admin/login`; the server then authenticates requests using a server-side session and session cookie. In LAN mode, credentials and session traffic travel over HTTP without TLS, so port `8788` must stay exclusively on a trusted private network. Never forward it from the router, expose it through a public tunnel/funnel, or use it on an untrusted network. The hardened HTTPS/reverse-proxy mode remains implemented as another supported deployment option tracked by #145. Runtime OAuth operations and scheduler token refreshes are serialized so reconnect/refresh cannot race against the same credential store.

## Roadmap

Version **0.1.0** is the current persistence/runtime baseline. `1.0.0` remains a later product
milestone for a sufficiently mature administration experience rather than a persistence-format
milestone.

### Current work

1. **Managed catalog validation** — operate Spain-focused Spanish plus the initial international
   playlists from the same TrueNAS instance, review sanitized diagnostics and refine source/policy
   choices from real operation.
2. **Administration maturity** — continue toward a UI that can manage playlists and supported
   Spotify-first sources without restoring a second runtime configuration path.
3. **Source/catalog expansion** — add more concise, deterministic sources and playlist definitions
   without duplicating the engine.
4. **Distribution readiness** — keep #297 non-blocking for private development while investigating a
   clearly supported low/zero-quota Spotify change signal before reconsidering broad public
   distribution.

### Historical implementation milestones

The original P0/P1 RSS/matching roadmap is retained only for provenance in the
[P1 umbrella issue #13](https://github.com/eXPerience83/news-bulletin-playlist/issues/13).
The current Spotify-first architecture supersedes that ingestion/matching path; issues #253-#258
record the detector, catalogue-ingestion, cutover and retirement work.

- **P0 — validated foundation** — provider contracts/watchdog, hardened container/TrueNAS runtime
  and Spotify catalogue/write probes.
- **P1 — completed historical implementation**:
  - [x] **P1.1 / #14** — source, canonical edition and playlist configuration/domain model; completed via #21.
  - [x] **P1.2 / #15** — shared RSS collection and canonical normalization; completed via #22.
  - [x] **P1.3 / #16** — SQLite persistence, migrations and 30-day operational retention; completed via #26.
  - [x] **P1.4 / #17** — deterministic source-to-Spotify episode matching; completed via #27.
  - [x] **P1.5 / #18** — desired-state generation and multi-playlist Spotify reconciliation; completed via #29.
  - [x] **P1.6 / #19** — production Spotify OAuth callback/token lifecycle through the private Web UI; completed via #30.
  - [x] **P1.7 / #20** — integrated engine cycle, scheduler and operational status in the durable runtime; completed via #31.

These historical bullets do **not** describe the current runtime data path or database schema.

Parallel/non-blocking product work such as the playlist cover-art system in #12 may land when its
configuration hook is stable, but it must never block bulletin synchronization.
