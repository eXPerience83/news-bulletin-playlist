# Spotify live behavior investigation — 2026-09-05

## Scope

This record preserves sanitized evidence gathered by running the real `news-bulletin-playlist`
application from source inside the isolated Remote Dev Codex environment against a disposable Spotify
LAB destination. No production TrueNAS data, production SQLite database, production OAuth state or
production playlist was mutated.

Initial runtime revision under test:

`90aeac0e1b43d39e9151b608fd3a6148cf98d027`

Runtime: CPython 3.14.7, editable source install.

Disposable LAB destination:

- Spotify playlist id: `2ai0Ky6cDiTLPHhF7pePxX`
- name during final observation: `NBP LAB - DO NOT USE - snapshot investigation R`
- granted scopes: `playlist-read-private`, `playlist-modify-private`, `playlist-modify-public`, `ugc-image-upload`

No OAuth URL/state, PKCE verifier, access token, refresh token, Authorization header, admin password or
credential-file contents are recorded here.

## Snapshot behavior

A 68-item playlist was used so reconciliation crossed Spotify's 50-item page boundary. The exact
readback paginated as 50 + 18 with zero unavailable slots.

Stable ordered-item fingerprint:

`6dbe237a1f9b0e4a0bb7c13ac6eb4fb94cff35f7874a7b7b3e33ecbc8c0e9a85`

Observed behavior:

| Operation | Snapshot effect | Item/order effect |
| --- | --- | --- |
| Replace 68 items | advanced | expected content change |
| Metadata only | advanced immediately | none |
| Cover only | advanced immediately | none |
| Combined admin metadata + cover | advanced | none |
| Manual Spotify-client privacy toggle | advanced | none |
| Web API `public=true` after client privacy | no observable change | none |
| Identical settled reconciliation | no write | exact 68-item read |
| Restart with an unexpected pending `C` | repeated `snapshot_error` | content remained exact |

Critical reproduced transition:

| Step | Baseline A | Expected B | Observed C | Content | Current-head result |
| --- | --- | --- | --- | --- | --- |
| Pending repair | `AAAACVD8…Zgug` | `AAAAC4PD…OJKA` | A | exact underneath, partial observation | applied, degraded |
| Metadata-only change | A | B | `AAAADGB/…Ld1D` | ordered fingerprint unchanged | `snapshot_error` |
| Process restart | A | B | C | exact 68-item read | repeated `snapshot_error`, no write |

Conclusion: Spotify `snapshot_id` is a playlist-wide version identifier. It cannot safely be treated as
an item-content-only version identifier. Metadata, cover and a manual privacy/access transition can
advance it without changing playlist items or ordering.

The safe reconciliation rule established by the live experiment is: an unexpected snapshot `C` during
a fresh pending A→B transition may be accepted only when the initial read is complete and exactly equals
the desired ordered URIs, a second complete ordered read remains exact, and the observed `C` remains
stable across that recheck. Partial reads, URI/order/count differences, destination changes, desired
fingerprint changes and unstable snapshots remain fail-closed.

Related issue: #134.

## Metadata attribution behavior

The production attributed description returned Spotify HTTP 400 in the LAB account. The existing
exact-400 fallback without attribution succeeded, and cover upload succeeded independently.

The current diagnostics do not distinguish ordinary attributed success from successful fallback without
attribution. That observability gap remains tracked in #133.

## Playlist visibility / access privacy behavior

The application called `create_private_playlist()` and sent `public=false`, but Spotify initially
reported `public=true`. A Web API update intended to set `public=false` was accepted while the observed
value remained `public=true`.

Using Spotify's client-side **Make private** action changed the playlist to actual invite-only access.
That manual action also advanced `snapshot_id` without changing the item/order fingerprint.

A final bounded inverse test was then run while the playlist had invite-only access. The Web API accepted
one `public=true` update with an empty success response, but the observed API value remained
`public=false` immediately and after 1 and 3 seconds. Snapshot `AAAADGB/9uXNfl9PzOIBWXHLTdWALd1D`, the
68-item count and ordered-item fingerprint all remained unchanged. This demonstrates that an accepted
`public=true` request did not make an observable visibility or content transition in this state.

Terminology for this project must keep the two dimensions separate:

- **Spotify API visibility**: the provider's `public` field/request. When discussed in the UI/docs use
  wording such as **visible in profile/search** vs **not listed in profile/search**, and treat it as a
  provider request/observation rather than an access-control guarantee.
- **Access privacy**: Spotify-client state such as **invite-only / restricted access**. The application
  must not claim that it can change this state through the Web API.

Accordingly, provider methods such as `create_private_playlist()` are misleading because they imply
access privacy. A neutral creation verb plus an explicit API-visibility request is preferable. Production
news playlists are intended to be visible/public-facing; future LAB tooling may request non-listed API
visibility and separately instruct the operator to enable invite-only access in Spotify when true access
restriction is required.

Follow-up is tracked separately in #135 so this provider semantic does not obscure the item
reconciliation fix.

## Live source → Spotify matcher contracts

Each built-in source was collected/normalized once and matched through its configured Spotify show using
the production bounded catalogue matcher.

| Source | Normalized | Catalogue calls | Matched | Pending | Ambiguous | Observed precision | Spotify duration min/median/max (s) |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `ser` | 585 | 2 | 95 | 490 | 0 | day:100 | 54 / 238 / 1203 |
| `rne` | 19 | 1 | 12 | 7 | 0 | day:12 | 238 / 271.5 / 492 |
| `ondacero` | 299 | 2 | 100 | 199 | 0 | day:100 | 162 / 290.5 / 384 |
| `abc` | 100 | 2 | 100 | 0 | 0 | day:100 | 230 / 296.5 / 478 |
| `cnn` | 1933 | 2 | 90 | 1843 | 0 | day:100 | 180 / 302.5 / 406 |
| `un_news_en` | 100 | 2 | 100 | 0 | 0 | day:100 | 180 / 284 / 360 |
| `rfi_fr` | 7 | 1 | 7 | 0 | 0 | day:10 | 600 / 600 / 1800 |
| `dlf_news` | 10 | 1 | 8 | 2 | 0 | day:10 | 250 / 311.5 / 612 |
| `rmf_fakty` | 41 | 1 | 39 | 2 | 0 | day:41 | 97 / 261 / 775 |

All configured Spotify show endpoints worked without retry or HTTP 429 during this investigation.
RFI admitted only the timestamped Journal Monde product; the observed `Tranche d'information` item was
rejected before Spotify matching.

All live Spotify candidates observed in this run used `release_date_precision=day`. A controlled matcher
case nevertheless proved that the previous generic `release_date_title` strategy could falsely accept a
same-title episode when Spotify supplied only `month` or `year` precision. The generic strategy therefore
requires day precision before a candidate can become `MATCHED`.

## Local fix validated during investigation

The isolated worktree used for the live investigation produced a local, unpushed fix touching:

- `src/news_bulletin_playlist/reconciliation.py`
- `src/news_bulletin_playlist/spotify/matcher.py`
- `tests/test_pending_snapshot_confirmation.py`
- `tests/test_release_date_title_strategy.py`

The reconciliation change accepts an unexpected pending `C` only after two complete exact ordered
readbacks with a stable snapshot. The matcher change requires day precision for the generic
`release_date_title` path.

Local validation reported:

- `python scripts/ruff_fix.py`: passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `mypy src`: passed
- `pytest`: **461 passed**

These local results are evidence, not repository acceptance. The exact fix still requires landing on PR
#130 and passing GitHub CI/Container before another candidate can be published.
