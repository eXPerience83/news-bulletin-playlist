# Controlled TrueNAS recovery candidate: Spotify null-slot handling

Status: **0.2.3b1 source candidate, NOT yet published or deployed**.
Operator production acceptance is tracked in #562 and #565.

## Scope

PR #561 added diagnostics and PR #563 added strict **add-only**
reconciliation from positively observed eligible items in a partial
Spotify catalogue. PR #567 adds only a read-only installation
identity preflight, not a startup hook. The candidate deliberately
retains 13 integrated sources, seven templates, the v10 managed-state
support and SQLite database format 3.

The optional-preset migration (#552), independent round-trip suite
(#553) and initial new-playlist cover automation (#528) remain unfinished.
They are not represented as implemented beta features and do not
delay an urgent, limited repair for existing managed destinations.

**Incomplete coverage is never permission for full reconciliation.**
Only qualified new episodes may be added by POST to an exactly read
destination. Existing entries must not be evicted, deleted, reordered
or replaced. When the configured cap would be exceeded, the correct
outcome is capacity-paused; an ambiguous POST is durably fenced against
blind retries. Normal reconciliation resumes after coverage is proven.

## Publication is separate from deployment

1. Confirm CI on the exact candidate HEAD: Python 3.15 pytest, Ruff,
   format, mypy and independent Container check. Record full SHA.
2. To test an unmerged candidate, the repository owner may comment
   the command /publish-candidate followed by the exact 40-character
   PR HEAD SHA on its open PR. The workflow builds and smoke-tests,
   then publishes immutable candidate-pr-PR-SHORTSHA and movable
   dev-amd64, **without updating edge/stable/latest**.
3. To publish an integrated candidate, manually run GitHub Actions
   **Publish edge AMD64** on the verified current main. The workflow
   smoke-tests and publishes immutable sha-FULLSHA plus edge and
   edge-amd64. Merge alone does NOT publish an image.
4. Pin any deployment to the **actual verified GHCR digest** or its
   documented immutable image reference. A Git commit SHA is not a
   Docker image digest. Publishing does not edit the TrueNAS YAML or
   restart the app.
5. Do not create a v0.2.3 stable Git tag, stable/latest alias or GitHub
   stable release as part of this beta. The Python version 0.2.3b1 is
   prerelease metadata, not stable publication.

See RELEASE_CHANNELS.md for channel invariants and the exact
operator authorization contract.

## Before a TrueNAS cutover

- Record the current running image ID/digest for exact rollback and
  record the new candidate source SHA and GHCR digest.
- Stop/quiesce writes and take a **complete consistent snapshot** of
  the TrueNAS dataset /mnt/Pool1/news-bulletin-playlist. A portable
  JSON or ZIP is NOT a backup of OAuth, SQLite, pending journals or
  Spotify destination identities.
- Record the four existing managed Spotify destination IDs, assigned
  sources, enabled status, names, covers, duration/retention/cap rules
  and item order. Do not paste credentials into a GitHub issue.
- Read current managed-state schema, SQLite database format and
  sanitized operational diagnostics. If an older image cannot read
  newly adopted schemas, image-only rollback is NOT guaranteed:
  restore a complete matching data snapshot as well.

## Safe observation sequence

1. Keep the dataset bind mount, ports, environment and service UID
   intact. Stop the existing app, snapshot persistent data, then
   update **only the image reference** to the verified candidate
   digest and start the single app instance. Never run a copy of the
   production OAuth/scheduler concurrently against live destinations.
2. Check /healthz, Admin and the reported exact build revision.
   Healthy HTTP status alone does not confirm RNE recovery.
3. Observe Noticias España with RNE show
   0UgidTKsoaHiHDARuPQNW1. A null-bearing catalogue must retain
   coverage_proven=false; if an eligible genuinely new episode is
   observed, the partial path may add it without deleting or
   reordering any older entry. No false full-reconcile attestation.
4. A destination already at its item cap may remain paused. This is
   expected safety behavior; **do not report the incident as fixed**
   merely because the container runs. Inspect diagnostics for
   capacity-paused and partial-add outcomes separately.
5. Confirm all four public destinations keep their original IDs and
   existing contents, and that EN, PL and long-form ES were neither
   recreated nor modified accidentally. Check subsequent cycles and
   a controlled restart for duplicate-POST or pending-journal errors.
6. For 429, transport errors or ambiguous write outcomes, verify
   the app does not blindly retry uncertain POSTs. If an operation
   remains fenced, investigate before manually changing anything.

## Stop/rollback conditions

If the source revision is wrong, state is unreadable, a playlist
identity/policy changes, the app makes destructive/duplicate writes,
or persistent authorization/startup failures appear: preserve
sanitized evidence, stop the app and restore the previously verified
image. When any incompatible persisted schema/content has changed,
restore its **matching complete** dataset snapshot too. Never reset
/data, delete pending intents, recreate Spotify playlists or clear
OAuth to force a retry.

Complete #562 with **actual production evidence**. Only then decide
in #565 whether this beta deserves a separately approved stable
release. Keep #552/#553 and #528 open for later product work.
