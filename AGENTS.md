# AGENTS.md

Instructions for AI coding/review agents working in this repository.

## Runtime contract

- **Development and required GitHub CI use CPython 3.15.x only.** The project metadata still declares `requires-python = ">=3.14,<3.16"` because the **published production Docker image remains pinned to CPython 3.14.8** pending a separate tested 3.15 Docker promotion (#494). CI is **not** proof of continued 3.14 package compatibility.
- Ruff `target-version = "py314"` and mypy `python_version = "3.14"` are deliberate lower-bound grammar/type checks to protect the existing Docker 3.14 runtime; do not move those to 3.15 until the runtime and declared minimum actually change.
- **PEP 758 is supported by the project grammar.** Python 3.14 allows `except A, B:` without an `as` clause. Do not report or rewrite it as invalid merely because an older parser rejects it.
- For Python syntax/compatibility evidence, use a 3.15-aware parser and actual 3.15 CI. For any behavior relying on 3.15-only language/library features, account for the still-deployed 3.14 Docker runtime. Preserve compatibility until a separately reviewed Docker migration or a deliberate packaging-contract change.

## Repository and Git safety

- When working under `/workspace`, set `GIT_CEILING_DIRECTORIES=/workspace` before destructive or branch-changing Git operations.
- Confirm `git rev-parse --show-toplevel` is the intended repository root. If it resolves to `/workspace`, stop rather than risking sibling projects.
- Do not use `git clean -fd`, `git clean -fdx` or `git reset --hard` unless the repository root and impact have been explicitly verified and the task genuinely requires it.
- Work on the requested feature/issue branch. Do not push directly to `main` and do not merge unless explicitly authorized.

## Required local validation

For normal source changes, run the repository gates with Python 3.15:

```text
python scripts/ruff_fix.py
ruff check .
ruff format --check .
mypy src
pytest
```

Treat CI and Container validation as independent evidence; do not claim local tests are CI.

## Commit and Actions discipline

- Keep commits coherent and reviewable, but avoid excessive micro-commits and push churn.
- Batch closely related edits into a meaningful checkpoint, inspect the complete diff, and run the relevant local validation before pushing.
- Do not create or push a new commit for every tiny correction, formatting fix, or intermediate implementation step when those edits can be validated together locally.
- Do not push knowingly broken or obviously incomplete intermediate states merely to use GitHub Actions as the development feedback loop.
- Prefer amending or folding unreviewed intermediate fixups when appropriate rather than creating a long chain of fixup commits.
- A push to an open PR can trigger multiple GitHub-hosted jobs. Treat unnecessary pushes as consuming a finite CI-cost resource, especially while the repository is private.
- Local validation is the normal feedback loop; GitHub Actions is the authoritative integration check at meaningful checkpoints.
- Before merging any PR, compare it with the latest `main`. If the PR is behind, rebase/update it and rerun the required CI. Check `main` again immediately before merge; if it advanced, repeat the update and CI cycle. `mergeable=true` and green CI do not prove that the PR is based on the latest `main`.
- This does not relax scope discipline: unrelated changes still belong in separate issues/PRs. Optimize push frequency, not change scope.

## Review discipline

- Reproduce or prove a finding before changing code. Classify it as a real bug, regression risk, hardening, observability improvement, test gap, or false positive.
- CI validates Python 3.15 only. Distinguish this development/test target from the pinned Python 3.14 production container; changes requiring Python 3.15 runtime must wait for #494. Do not claim 3.14 CI coverage.
- Static-analysis findings are leads, not proof. Check reachability, runtime contract and actual data flow.
- Prefer the smallest change that fixes the demonstrated problem and add a regression test.
- Preserve fail-closed behavior around persistence, Spotify reconciliation, OAuth and diagnostics.
- Do not broaden the scope of a PR with unrelated cleanup.

## Spotify and live-system safety

- Unit/integration tests must not perform real network or OAuth operations.
- Local base descriptions retain the 248-character contract. Explicit metadata sync adds the public-repository footer (110-character suffix) and rejects bases above 190 before metadata PUT, preserving local text and independent cover upload. No cosmetic metadata writes on load, ordinary Save or activation.
- Use the disposable LAB environment for destructive Spotify experiments when explicitly requested.
- Production Spotify playlists, OAuth state, `/data`, TrueNAS datasets, metadata and covers must not be modified without explicit operator authorization.
- Read-only production inspection is acceptable only when the task requires it and credentials are already available through the approved environment.
- Treat mutating Spotify 5xx/transport failures as ambiguous until readback proves the resulting state.
- A 429 uses the repository's global Spotify cooldown semantics; do not add ad-hoc retries that bypass them.

## Secrets and diagnostics

Never log, commit, persist in diagnostics, or expose in user-visible errors:

- access tokens;
- refresh tokens;
- Authorization headers;
- authorization codes;
- PKCE verifiers;
- admin passwords or other credentials;
- raw authenticated provider/OAuth response bodies.

One narrow production exception is allowed for #266-style public detector schema drift: the
unauthenticated Spotify Embed/oEmbed response body may be retained, bounded by the detector response
limit, on the first response-contract failure in a consecutive degraded streak. That capture must
never include request headers/cookies or OAuth material, must not be printed to stdout or rendered
inline in the normal diagnostics table, and may be exposed only through authenticated diagnostic
evidence such as the ZIP export. Repeated failures must not create a capture storm.

When temporary raw instrumentation outside that narrow exception is needed for a LAB investigation,
keep it outside committed/product diagnostics, sanitize the final report, and remove temporary
dumps/debugging before finishing.

Diagnostics should otherwise prefer bounded, allowlisted metadata that is sufficient to reconstruct
an incident without exposing raw URI lists or credentials.
For bounded diagnostics exports, derive the exported rows and `truncated` from one SQLite read snapshot; do not combine separately committed list and count queries.

## Architecture invariants

- Portable source JSON v1 is a pure configuration interchange, separate from managed-state
  persistence and production backups. Export effective source definitions with their exact
  processing capabilities; never substitute a generic parser or silently omit a filter.
  Decoding is not installation. Canonical Spotify show ID governs future deduplication;
  imported provenance/local IDs never authorize overwriting local definitions. See
  `docs/PORTABILITY.md` for the contract and staged #542 migration boundaries.
  Admin source downloads use `load_readonly()` under the configuration lock; they
  must not persist legacy migrations, finalize provisioning or require Spotify/OAuth.
- Installed source definitions use explicit managed-state schema v8 and the portable v1
  capability contract. Legacy manual `ManagedSource` intake remains restricted; `InstalledSource`
  persistence does not authorize ordinary Admin creation with specialized parsers/filters.
  Never silently upgrade v6/v7 to v8 on read/startup/export/save. New drafts preserve an
  already-v8 schema. Old images cannot read v8: rollback requires a matching data snapshot.
  See `docs/PORTABILITY.md` for the source representation and compatibility matrix.
- Source import preview/apply uses bounded session-only uploads, `load_readonly()` and
  the configuration lock; never use provider-backed intake or provisioning snapshots.
  Canonical show identity determines reuse, conflicts allow keep-local/cancel only,
  and every apply rechecks full configuration/pending-provision fingerprints. Reuse-only
  imports never save; only explicit new-source confirmation may opt into v8. On save
  errors read back before claiming a commit or unchanged state; never retry blindly.
- Portable playlist ZIP v1 exports require persisted per-object UUIDv4 identities in
  explicit managed-state schema v9. Legacy identity preparation is a separate authenticated,
  CSRF-protected confirmed mutation with rollback acknowledgement. Never generate identities
  or migrate on GET/read/startup. Preserve v9 in ordinary saves and source imports. Drafts
  and managed destinations have distinct identities; edits retain them. Downloads include
  durable disabled-source assignments and exact selected JPEG bytes under the configuration
  lock with `load_readonly()`, never provisioning snapshots, provider calls or scheduler wake.
  Pre-v9 images reject v9; rollback needs a matching complete backup. See `docs/PORTABILITY.md`.
- Portable playlist ZIP import is preview-first and installs only local drafts. Reuse
  pure source planning, never source Apply (one joint managed-state save only). Under
  the configuration lock revalidate the exact session-bound ZIP, state/catalog/pending
  fingerprint, mappings and conflicts; prepare verified byte-preserving content-addressed
  JPEGs before JSON publication. Never extract ZIP paths, overwrite existing assets,
  finalize provisioning or call provider/OAuth/SQLite/scheduler. Unknown capabilities
  block the whole package. UUID/content idempotence is cross draft/managed/quarantine.
  Imported provenance/exceptions require explicit schema v10 adoption, not a silent v9
  extension. Reads/exports/previews/legacy saves do not adopt v10; all later mutations
  preserve it. Pre-v10 images need a matching backup for rollback. Imported drafts keep
  exact seconds/legacy policies and are edit/export only; activation must be rejected
  before OAuth. On ambiguous state saves read back once; never retry blindly. Safe
  unreferenced prepared assets can remain and be verified/reused on a fresh preview.
- One engine can drive multiple playlists, countries and languages.
- The authoritative production architecture is Spotify-first. Runtime playlist decisions must not
  depend on RSS collection, RSS-native identities, RSS-to-Spotify matching or PENDING/AMBIGUOUS
  matching retries.
- An unauthenticated public Spotify show observation drives the change signal. Its already-observed
  aware exact release timestamp may supply lower-priority chronology only after exact source/show/
  episode identity, official catalogue period and bounded future validation. Semantic TITLE wins.
  The official Spotify Web API catalogue remains the episode-data authority for the refreshed source.
- The public Embed/oEmbed surfaces are never the episode catalogue/content source. Do not scrape a
  historical episode list from HTML; persist only the bounded latest-episode detector evidence
  required to decide whether an official catalogue refresh is needed.
- Spotify episode ID/URI is the canonical runtime episode identity. Do
  not create a new RSS identity that then needs matching back to Spotify.
- A normal detector change refreshes only the changed source catalogue. Reuse persisted state for
  unchanged sources and apply playlist retention/ordering locally; do not fan out into all source
  catalogues before an ordinary write.
- Each source catalogue refresh must commit operational episode rows, accepted history and source
  refresh/coverage authority in one SQLite transaction (`apply_source_refresh`). Never commit rows
  separately from trust in engine refresh or stale escalation paths.
- Literal null catalogue slots are unknown identities: retain original provider positions and only
  known episodes, force incomplete/unproven coverage and suppress API latest identity even after
  public enrichment. Unproven sources cannot authorize any dependent destination reconciliation.
  Arrays, strings, numbers, booleans and malformed episode objects remain fail-closed.
- Independent detector audits are inactivity-triggered, not fixed-period while normal transitions
  are occurring. A detector or audit failure must preserve last-known-good state.
- Deterministic stale/unavailable desired-item evidence may escalate to refreshing the other
  catalogues for the affected playlist. Ambiguous mutating transport/5xx outcomes still require
  existing readback/snapshot confirmation before retry; 429 still uses the global cooldown.
- Universal semantic title detection is runtime temporal authority, independent of source/parser
  identity. Use source IANA timezone, official catalogue date compatibility and unique DST wall-time
  resolution. Unreviewed structures require bounded corroborating catalogue evidence; token discovery
  alone is not proof. Preserve explicit parsing provenance and retain episodes through the existing
  catalogue/FIRST_SEEN fallback when evidence is insufficient. Never invent minute precision.
- Exact TITLE/PUBLIC playlist ordering uses the physical UTC `edition_at` instant. FIRST_SEEN/coarse
  fallback ordering uses one coherent physical upper bound: the earlier of the real `first_seen_at`
  instant and the end of the source-local Spotify release period converted through the source IANA
  timezone/DST rules. Never splice a catalogue calendar date onto an observation clock. This bound is
  ordering-only: it must not create `edition_at`, upgrade provenance, or change retention semantics.
- Historical parser IDs/layout adapters remain compatibility contracts, not per-source runtime
  dispatch. Temporal evidence and collection filtering must remain independent.
- Temporal detection defaults to AUTO inferred exclusively from the current source catalogue.
  Never inject historical broadcaster layouts globally. Optional temporal_strategy capabilities
  apply after AUTO and must be reusable, bounded and product-independent; disabled preserves fallback.
- PublicReleaseMode (instant/wall_time/disabled) is independent of TemporalStrategy.
  Wall time uses source IANA timezone and shared DST-safe unique resolution, preserving seconds
  and microseconds. Accepted PUBLIC history is forward-only; disabled prevents current reuse at
  the next natural refresh without deleting history. Re-enable may reuse the original instant
  only for incoming FIRST_SEEN; newly validated PUBLIC replaces older PUBLIC history.
- Do not download or store audio.
- Persisted Spotify episode URIs must satisfy the shared episode-URI invariant before reuse.
- A failed source or destination must not unnecessarily block independent sources/playlists.
- ManagedPlaylist.source_ids is durable assignment intent. Effective playlist membership is
  assigned AND source.enabled; disabled assignments are dormant and return on re-enable unless
  explicitly unassigned. Only configured Disable authorizes exclusion; provider uncertainty
  remains fail-closed. Active playlists require at least one enabled assigned source; disabling
  the last effective source is rejected atomically, including pending activation. A mid-cycle
  configuration change must still prevent writes under the old playlist contract.
  Runtime loading atomically pauses previously valid active playlists whose known assignments
  are all disabled, retaining source_ids and destinations without provider I/O. This durable
  recovery keeps Admin available with schema v6 unchanged; activation after repair is explicit
  and still requires an enabled assigned source. Compilation itself remains strict.
- Database format 1 is the supported Spotify-first persistence baseline; format 2 adds separate
  accepted-episode history; current format 3 widens episode/history provenance to validated public
  release timestamps through a transactional forward migration. Managed-state JSON uses schema v6
  with atomic v1/v2/v3/v4/v5 upgrades (missing playlist minimum defaults to 0 seconds), absent
  temporal_strategy defaulting to AUTO and absent public_release_mode defaulting to instant.
  Built-in overrides persist only source ID and public mode; built-in definitions remain image-owned. The database filename remains
  `/data/news-bulletin-playlist-v1.sqlite3`. Pre-0.1 private-development SQLite tables/migrations
  are not runtime compatibility targets; future persistence changes need reviewed forward migrations.

- The offline v11 inventory compares file bytes and file/directory metadata (UID/GID, POSIX mode, xattr digests); never expose xattr values. This is an evidence gate, not a complete ZFS ACL backup, SQLite snapshot, or permission to migrate. Preserve fail-closed shadow/cutover behavior.

- Offline SQLite backup validation is an isolated, opt-in verification step only. Open SQLite exclusively on a disposable, verified copy of the independent backup, not production /data or the backup itself. A successful integrity_check does not certify complete WAL capture, service quiescence or TrueNAS ACL restore. Preserve the old-image full-dataset rollback requirement and keep v11 cutover disabled.

- Synthetic legacy rollback rehearsals may inspect restored /data with the read-only old-schema parser and a frozen historical catalog, but must compare full backup contents first. They are **not** evidence that an older published container/image can boot after schema v11; that requires a separately pinned, disconnected old-image rehearsal. Do not infer cutover permission from a passing test.

- Owner decision 2026-10-31: migration to v11 is forward-only and BREAKING (no supported downgrade); old-image rollback and full ACL snapshot rehearsal are not release gates. Keep v11 atomic, retain legacy bytes inert, fail closed on corrupt/newer state, test preservation of all installed destinations and provider-free upgrade. The isolated v11 writer must not be wired into startup until all legacy reads/writes are replaced consistently. Never touch deployed TrueNAS or Spotify without explicit operator deployment authorization.

## Documentation and external contracts

- For Spotify endpoint/scopes/limits/response-contract claims, verify current official Spotify documentation before treating a claim as a defect.
- Treat database format 1 / 0.1.0 as the first supported persistence compatibility baseline. Pre-0.1 RSS/matcher/planner documents and schemas are historical provenance, not upgrade targets for the current database.
- The owner explicitly decided on 2026-10-12 to publish the project software under **MIT**, with `Copyright (c) 2026 Roberto Ribes`. Preserve the LICENSE, Python package/image license metadata and author notices. Do not assume MIT grants rights to Spotify data, third-party marks or unreviewed cover assets. Public repository visibility requires a separate security/history and asset-provenance review.
- Keep `pyproject.toml`, 3.15-only CI, the still-pinned 3.14 production Docker image, and review-agent configuration consistent. The declared 3.14 lower bound remains until a separately validated Docker and package migration.
- Keep the README **Playlists** index synchronized in the same change whenever a public production playlist is added, removed, renamed or replaced. Never list private, test or merely built-in-but-unpublished destinations.
- Root `AGENTS.md` applies repository-wide and should be updated when an invariant changes materially.
