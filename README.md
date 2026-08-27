# News Bulletin Playlist

Dynamic Spanish-language news bulletin playlist for Spotify.

> Early research/prototype stage. This project is not affiliated with or endorsed by Spotify or any news provider.

## Goal

Keep a playlist automatically populated with the most recently published Spanish-language news bulletins from selected Spanish and international providers.

Initial product invariants:

- keep episodes published within the last **48 hours**;
- cap the playlist at **100 episodes**;
- order by source publication timestamp (`published_at`), newest first;
- retain operational metadata locally for **30 days**;
- never download or store podcast audio;
- treat RSS/provider metadata as the timing source and Spotify as the playlist destination.

## P0 providers

| Provider | Parser | Spotify show identified | Status |
| --- | --- | --- | --- |
| Cadena SER | ✅ | ✅ | core |
| RNE | ✅ | ✅ | core |
| Onda Cero | ✅ | ✅ | core |
| CNN 5 Cosas | ✅ | ✅ | core international |
| COPE | ✅ national title contract | ⚠️ authenticated lookup pending | candidate |

See [`docs/P0_FINDINGS.md`](docs/P0_FINDINGS.md) for the source research and the remaining authenticated Spotify probe.

## Development

The current P0 code has no runtime dependencies. Development checks use pytest, Ruff and mypy.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check .
mypy src
```

CI runs the parser contract suite on Python 3.12 and 3.14.

## Security

Do not commit Spotify client secrets, refresh tokens, `.env` files or the runtime database. Spotify credentials will be stored only in the deployment runtime once OAuth work begins.

## Roadmap

1. **P0** — provider contracts and authenticated Spotify catalogue probe.
2. **P1** — feed collection, canonical metadata model and SQLite persistence.
3. **P2** — RSS-to-Spotify matcher and reconciliation rules.
4. **P3** — private playlist write/readback and idempotency.
5. **P4** — provider watchdog via GitHub Actions.
6. **P5** — hardened Docker runtime and private admin UI.
7. **P6** — public playlist / first release.
