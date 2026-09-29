# AGENTS.md

Instructions for AI coding/review agents working in this repository.

## Runtime contract

- The supported interpreter is **CPython 3.14.x only**.
- `pyproject.toml` is authoritative and declares `requires-python = ">=3.14,<3.15"`.
- Ruff targets `py314`; mypy targets Python `3.14`; CI runs Python `3.14`; the production image uses CPython 3.14.x.
- Interpret syntax, AST, typing and compatibility using the **Python 3.14 grammar**.
- **PEP 758 is part of the contract.** In Python 3.14, `except A, B:` is valid when there is no `as` clause. Do not rewrite valid PEP 758 syntax merely to satisfy an older parser.
- Before using `python`, `python3`, `ast.parse`, `compile`, a linter or a static analyzer as evidence of a syntax/compatibility defect, verify that it is actually using Python 3.14 semantics. If the local/sandbox parser is older, its result is non-authoritative; verify with the repository CI/container or a CPython 3.14 environment instead.

## Repository and Git safety

- When working under `/workspace`, set `GIT_CEILING_DIRECTORIES=/workspace` before destructive or branch-changing Git operations.
- Confirm `git rev-parse --show-toplevel` is the intended repository root. If it resolves to `/workspace`, stop rather than risking sibling projects.
- Do not use `git clean -fd`, `git clean -fdx` or `git reset --hard` unless the repository root and impact have been explicitly verified and the task genuinely requires it.
- Work on the requested feature/issue branch. Do not push directly to `main` and do not merge unless explicitly authorized.

## Required local validation

For normal source changes, run the repository gates with Python 3.14:

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
- This does not relax scope discipline: unrelated changes still belong in separate issues/PRs. Optimize push frequency, not change scope.

## Review discipline

- Reproduce or prove a finding before changing code. Classify it as a real bug, regression risk, hardening, observability improvement, test gap, or false positive.
- Do not make compatibility changes for unsupported Python versions or toolchains.
- Static-analysis findings are leads, not proof. Check reachability, runtime contract and actual data flow.
- Prefer the smallest change that fixes the demonstrated problem and add a regression test.
- Preserve fail-closed behavior around persistence, Spotify reconciliation, OAuth and diagnostics.
- Do not broaden the scope of a PR with unrelated cleanup.

## Spotify and live-system safety

- Unit/integration tests must not perform real network or OAuth operations.
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

- One engine can drive multiple playlists, countries and languages.
- The authoritative production architecture is Spotify-first. Runtime playlist decisions must not
  depend on RSS collection, RSS-native identities, RSS-to-Spotify matching or PENDING/AMBIGUOUS
  matching retries.
- An unauthenticated public Spotify show observation is
  used only as a **change signal**; when that episode identity changes, the official Spotify Web API
  catalogue is the episode-data authority for the refreshed source.
- The public Embed/oEmbed surfaces are never the episode catalogue/content source. Do not scrape a
  historical episode list from HTML; persist only the bounded latest-episode detector evidence
  required to decide whether an official catalogue refresh is needed.
- Spotify episode ID/URI is the canonical runtime episode identity. Do
  not create a new RSS identity that then needs matching back to Spotify.
- A normal detector change refreshes only the changed source catalogue. Reuse persisted state for
  unchanged sources and apply playlist retention/ordering locally; do not fan out into all source
  catalogues before an ordinary write.
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
- Historical parser IDs/layout adapters remain compatibility contracts, not per-source runtime
  dispatch. Temporal evidence and collection filtering must remain independent.
- Do not download or store audio.
- Persisted Spotify episode URIs must satisfy the shared episode-URI invariant before reuse.
- A failed source or destination must not unnecessarily block independent sources/playlists.
- Database format 1 is the supported Spotify-first persistence baseline; current format 2 adds
  separate accepted-episode history via an explicit forward migration. The database filename remains
  `/data/news-bulletin-playlist-v1.sqlite3`. Pre-0.1 private-development SQLite tables/migrations
  are not runtime compatibility targets; future persistence changes need reviewed forward migrations.

## Documentation and external contracts

- For Spotify endpoint/scopes/limits/response-contract claims, verify current official Spotify documentation before treating a claim as a defect.
- Treat database format 1 / 0.1.0 as the first supported persistence compatibility baseline. Pre-0.1 RSS/matcher/planner documents and schemas are historical provenance, not upgrade targets for the current database.
- The current repository state is **All rights reserved**. Do not add or restore an open-source license, license file or package/image license metadata unless the owner explicitly changes that decision. Historical copies already distributed under MIT keep the rights granted to those copies.
- Keep `pyproject.toml`, CI, Docker runtime and review-agent configuration consistent about the supported Python version.
- Root `AGENTS.md` applies repository-wide and should be updated when an invariant changes materially.
