# News Bulletin Playlist

Open-source service for dynamic news bulletin playlists across countries and languages,
with Spotify as the first destination.

> Early research/prototype stage. This project is not affiliated with or endorsed by Spotify or any news provider.

## Goal

Run **one engine that can maintain multiple news-bulletin playlists** from shared provider data.

Spain / Spanish-language news is the first implementation and validation target. The architecture is intentionally multi-playlist, multi-country and multi-language so the same runtime can later power English, French, German, Polish and other European playlists without duplicating the application.

Core architectural invariant:

> **Fetch once -> normalize once -> store once -> distribute to many playlists.**

A provider/source is independent from a playlist and may feed several playlists. A single engine run should fetch each required source once, normalize it once, and then reconcile every configured destination playlist independently.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the architectural contract and invariants.

Initial product defaults:

- keep episodes published within the last **48 hours**;
- cap a playlist at **100 episodes**;
- order by source publication timestamp (`published_at`), newest first;
- retain operational metadata locally for **30 days**;
- never download or store podcast audio;
- treat RSS/provider metadata as the timing source and Spotify as the playlist destination;
- allow playlist-specific policies to override defaults when required.

## Editorial independence

Playlist inclusion and ordering are based on source/editorial rules, freshness and playlist policy — **never on payment, donations, sponsorship or other compensation**.

Paid playlist placement, guaranteed inclusion, paid priority and sponsor-funded changes to playlist content are explicitly outside the product design.

Possible future monetization of the software or a separate qualifying non-streaming service is a different concern and must remain separate from playlist composition.

## Spotify access model

As of 2026-08-27, Spotify Development Mode is intended for learning, experimentation and personal **non-commercial** projects. The current deployment is therefore treated as non-commercial.

Spotify's Developer Policy permits certain limited commercial uses for qualifying non-streaming applications, but any future commercial use would require re-checking Spotify's then-current access mode, eligibility requirements and policies. Development Mode must not be used as the assumed foundation for building or scaling a business.

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the current Spotify platform constraints and the authenticated probes still required.

## P0 providers

The initial provider research focuses on the first Spain / Spanish-language playlist.

| Provider | Parser | Spotify show identified | Status |
| --- | --- | --- | --- |
| Cadena SER | ✅ | ✅ | core |
| RNE | ✅ | ✅ | core |
| Onda Cero | ✅ | ✅ | core |
| CNN 5 Cosas | ✅ | ✅ | core international |
| COPE | ✅ national title contract | ⚠️ authenticated lookup pending | candidate |

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the source research and the remaining authenticated Spotify probe.

## Container runtime

The supported runtime is a hardened Python 3.14 container designed for one engine that can later manage many playlists. The container uses a read-only root filesystem, runs as a non-root numeric UID/GID, keeps `/data` as its only persistent writable path and drops Linux capabilities. It serves an internal Docker health endpoint plus a read-only status page.

For local Docker:

```bash
docker compose up --build -d
docker compose ps
```

The local status page is available only on `http://127.0.0.1:8788/`.

For TrueNAS 26-BETA.3 or newer, create a dedicated dataset with the **Apps** preset and install [`deploy/truenas.yaml`](deploy/truenas.yaml) as a **Custom App via YAML**, not as a Community catalog app. The YAML publishes the status UI on port `8788` and includes best-effort `x-portals` metadata. Direct access to `http://<truenas-host>:8788/` remains the supported path. See [`docs/DEPLOY_TRUENAS.md`](docs/DEPLOY_TRUENAS.md) for the short installation procedure and image-integrity details.

## Development

The project targets **Python 3.14**. PyYAML is the only runtime dependency; development checks use pytest, Ruff and mypy.

```bash
python3.14 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
mypy src
```

CI runs Ruff, mypy and pytest on Python 3.14. A separate container workflow validates Docker/Compose changes and publishes successful `main` builds to GHCR.

## Domain and configuration contract

P1 configuration is YAML and separates global source definitions from playlist policies and
destination references. [`config/news-bulletin-playlist.example.yaml`](config/news-bulletin-playlist.example.yaml)
is a non-production example: its Spotify destination must be replaced with an already provisioned
playlist ID before later runtime integration. Issue #14 does not provision a playlist.

The example uses the verified feeds and existing source IDs `ser`, `rne`, `ondacero` and `cnn`.
COPE remains a disabled candidate without an invented endpoint or Spotify catalogue reference.
Playlist `source_selection.explicit` is authoritative in schema version 1; playlist countries and
languages describe editorial scope and do not implicitly filter that list. This is why a
Spain-oriented playlist can explicitly include the US source CNN 5 Cosas.

Canonical editions are identified only by `(source_id, source_native_id)`. Titles and timestamps
are metadata, never identity. Canonical timestamps are timezone-aware UTC values. Spotify show
references are source catalogue metadata and are intentionally distinct from writable playlist
destinations.

## License

Released under the [MIT License](LICENSE).

## Security

Do not commit Spotify client secrets, refresh tokens, `.env` files or the runtime database. Spotify credentials will be stored only in the deployment runtime once OAuth work begins.

## Roadmap

1. **P0** — provider contracts, hardened container/TrueNAS runtime and authenticated Spotify catalogue/write probes for the first Spain / Spanish-language playlist.
2. **P1** — shared feed collection, canonical metadata model, playlist configuration and SQLite persistence.
3. **P2** — RSS-to-Spotify matcher and reconciliation rules shared across playlists.
4. **P3** — multi-playlist reconciliation and idempotent scheduled engine cycles.
5. **P4** — provider watchdog via GitHub Actions.
6. **P5** — private admin UI, production token persistence and operational hardening.
7. **P6** — public first playlist / first release.
8. **P7** — expand sources and playlist definitions to additional languages and European countries.
