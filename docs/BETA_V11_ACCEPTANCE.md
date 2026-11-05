# Forward-only v11 / 0.3.0b1: controlled beta acceptance

**Status (2 November 2026): source candidate only; not published or deployed.** The Python package version `0.3.0b1` is staged in unreleased `main`. No beta image, Git tag, GitHub Release, edge/stable/latest movement, TrueNAS upgrade or real Spotify acceptance follows from merging code. Track the release in [#565](https://github.com/eXPerience83/news-bulletin-playlist/issues/565).

## Release boundary: BREAKING / no supported downgrade

- **Fresh v11** installations start with zero installed sources, templates, drafts and Spotify destinations. Thirteen image-owned source examples and seven playlist examples are **read-only until explicitly selected**. A source preset is downloaded and imported through preview/confirm; a playlist example becomes a local draft only after its required sources are installed and enabled. These actions are provider-free. Creating a real Spotify playlist is a separate explicit activation.
- **Existing v6–v10** installations migrate once to the owner-controlled `/data/managed-state-v11.json` authority, keeping their frozen source/template definitions, existing Spotify destination IDs, assignments, managed policies, drafts, cover references and pending/quarantine records. Historical `/data/managed-state.json` remains **inert evidence**; it is not a fallback if v11 is damaged.
- The transition is **forward-only**. Older images do not support v11 and image-only rollback is **not** a recovery path. Do not downgrade, delete a v11 witness, edit schema versions, reimport legacy state, recreate destinations or reset OAuth to repair a failed cutover.
- Source JSON and playlist ZIP are portable configuration artifacts, **not** backups of OAuth, Spotify destination IDs, SQLite/WAL, journals or cover assets. The complete independent `/data` copy is a separately planned operator safeguard, not a promise of automated rollback.

## Completed isolated gates (not production acceptance)

- PRs #585–#587: forward-only runtime/Admin, truly empty fresh install, optional source presets and playlist-example-to-local-draft flow.
- PR #589 / #553: synthetic v6–v10 migration and four preserved destination IDs, isolated SQLite/WAL, portable JSON/ZIP between independent data roots, idempotence, corruption and stale-write failures.
- PR #590 / #528: initial Spotify creation description includes bounded attribution; selected cover is attempted **only after** the destination is durably known. Failed cover, 429/5xx/transport and uncertain provisioning cannot trigger duplicate playlist creation; later metadata/cover synchronization is explicit.
- #527 owner cover UI and asset store already exist in code: authenticated bounded upload and private preview, local draft/managed selection, return to bundled/default cover, no upload on local Save, safe content-addressed ownership. Actual persistent TrueNAS uploads and real Spotify behavior remain unaccepted.
- #562 add-only degraded catalogue logic is covered by isolated tests. A `null` slot is not proof of absence; additions use only positively observed eligible IDs, cannot exceed cap or evict/reorder old items, and uncertain POSTs are fenced. This **does not** prove the live RNE incident is resolved.

## Before any separately authorized publication

1. Freeze and record the **exact current `main` commit SHA**, package version and all intended changes. Check that CI (Python 3.15, Ruff, formatting, mypy, pytest) and the **independent Container** workflow are green for that exact revision. Check no unresolved review/blocking findings and rerun after any new commit.
2. Validate that the candidate Dockerfile uses the intended pinned official Python **3.15.2** image digest and that the container smoke test covers startup, web health and durable restart. The **previously published** production image may still use Python 3.14.8. Declaring package support for 3.14 and using Ruff/mypy 3.14 compatibility checks is **not** the same as running 3.14 in current CI.
3. Review the breaking notes, v11 source/playlist opt-in UI, issue #527/#528 outcomes and #562 negative cases (capacity, 429, ambiguous POST, stale partial catalogue, exact readback). Existing four real playlists must never be treated as disposable test fixtures.
4. Obtain **explicit operator permission** before any GHCR workflow, release/tag, TrueNAS update, OAuth activity or real Spotify write. The normal **Publish edge AMD64** workflow moves `edge`/`edge-amd64`, not merely an immutable beta-only channel. The PR-candidate workflow is for a reviewed **open PR**, not already merged `main`. Do not silently invoke either as a substitute for approval. Do **not** create a stable `vMAJOR.MINOR.PATCH` tag or move `stable`/`latest` for this beta.

For an interrupted authority publication or an uncertain Spotify mutation, follow the [v11 upgrade and incident runbook](V11_UPGRADE_INCIDENT_RUNBOOK.md) **before** any retry or new instance. Its steps are for operator review and do not authorize production changes.

## Separate operator-controlled TrueNAS acceptance (NOT executed)

1. Record running image ID/digest and sanitized installed state, including the **four existing Spotify destination IDs**, source assignments, descriptions, cover choices, duration/cap/retention policies and pending journals. Never put OAuth tokens, secrets or raw private `/data` in issues.
2. Stop/quiesce the application, ensure no second worker shares the production dataset or OAuth/scheduler, and take an **independent complete, consistent `/data` backup** preserving the state files, SQLite database plus WAL/SHM, OAuth, provisioning/snapshot journals, uploaded covers and relevant ownership/permissions/metadata. A portable ZIP/JSON export is insufficient. Test backup integrity independently; a health check alone does not prove recoverability.
3. Only after an explicit release/deploy decision, pin a **verified GHCR image digest** or immutable candidate reference (a Git commit is not an image digest), update the approved TrueNAS image and start exactly one instance. Verify build identity and HTTP health **plus** the v11 authoritative state and preserved legacy evidence, no duplicate provisioning, all four destination IDs unchanged, and no missing or substituted cover/assets.
4. Perform scoped real acceptance **only with approval**. For #562 inspect RNE `0UgidTKsoaHiHDARuPQNW1`: a `null`-bearing catalogue retains unproven coverage; qualified add-only writes, cap-paused outcomes and recovery to normal reconciliation must be distinguished in diagnostics, with no destructive operations. For #527, test a new local draft's owner JPEG upload/preview/restart/switch-back on the persistent mount. For #528, if approved, create **one disposable new Spotify destination**, verify initial attribution and selected cover, and confirm that failure/retry cannot create duplicates. Never use the four established public destinations for destructive acceptance.
5. Observe follow-up cycles, restarts, 429 cooldown and pending/snapshot behavior; document only sanitized evidence. Mark #527/#528/#562 completed **only after** their relevant real acceptance, then separately decide #565 beta/stable promotion.

## On failure

Stop the candidate and **preserve** all current v11 state, logs and journals for investigation. Do not treat reverting an image tag as an automatic rollback or delete a v11 authority/witness to trick an older image into loading. Recovery from a partial cutover requires an independently reviewed operator decision using the verified backup and incident evidence; **there is no supported old-image downgrade path**. Do not publish stable, change the four existing destinations or clear credentials to force another try.

See [release channel semantics](RELEASE_CHANNELS.md), [TrueNAS installation](DEPLOY_TRUENAS.md), [source/ZIP portability](PORTABILITY.md) and [cover storage](CUSTOM_COVER_ASSETS.md).
