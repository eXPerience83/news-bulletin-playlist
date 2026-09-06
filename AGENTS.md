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
- raw provider response bodies unless a temporary isolated investigation explicitly requires them.

When temporary raw provider instrumentation is needed for a LAB investigation, keep it outside committed/product diagnostics, sanitize the final report, and remove temporary dumps/debugging before finishing.

Diagnostics should prefer bounded, allowlisted metadata that is sufficient to reconstruct an incident without exposing raw URI lists or credentials.
For bounded diagnostics exports, derive the exported rows and `truncated` from one SQLite read snapshot; do not combine separately committed list and count queries.

## Architecture invariants

- One engine can drive multiple playlists, countries and languages.
- Fetch once -> normalize once -> store once -> distribute to many playlists.
- RSS/provider data is the temporal authority; Spotify is the current destination, not the product domain.
- Do not download or store audio.
- Persisted `MATCHED` Spotify episode URIs must satisfy the shared episode-URI invariant before reuse.
- A failed source or destination must not unnecessarily block independent sources/playlists.

## Documentation and external contracts

- For Spotify endpoint/scopes/limits/response-contract claims, verify current official Spotify documentation before treating a claim as a defect.
- Keep `pyproject.toml`, CI, Docker runtime and review-agent configuration consistent about the supported Python version.
- Root `AGENTS.md` applies repository-wide and should be updated when an invariant changes materially.
