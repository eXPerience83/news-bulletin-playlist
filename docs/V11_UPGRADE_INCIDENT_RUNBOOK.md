# v11 upgrade and Spotify-write uncertainty: operator incident runbook

**Status:** predeployment draft for review, not a production recovery authorization. Applies to a candidate upgrading an existing `0.2.3b1` (v6–v10) installation to forward-only v11. See [beta acceptance](BETA_V11_ACCEPTANCE.md), [TrueNAS deployment](DEPLOY_TRUENAS.md), [#565](https://github.com/eXPerience83/news-bulletin-playlist/issues/565) and [#421](https://github.com/eXPerience83/news-bulletin-playlist/issues/421).

**Operator decision (2026-11-05): proceed without a private/full `/data` backup or isolated copy-migration rehearsal.** This deliberately accepts possible loss of local configuration, Spotify destination bindings, OAuth state, custom covers and SQLite/journals. Recreating sources and playlists can restore functionality, **not necessarily original Spotify IDs, links, history, artwork or byte-identical settings**. The generic Admin activation creates a new playlist; adopting an existing ID is limited to an unresolved provisioning intent, not general recovery.

**No automatic rollback.** Neither an old container image nor portable source JSON / playlist ZIP can restore a v11 installation. The four existing Spotify destinations must not be used as destructive test fixtures. This document provides decisions and evidence to collect; it does not claim that Spotify can cancel a previously transmitted mutation.

## Gate 0 — before authorizing the first candidate boot

1. Record the **actually running** old image ID/digest, application build, effective TrueNAS configuration and runtime stop/restart policy. The source repository's Dockerfile and YAML do not prove what TrueNAS is running. Capture only sanitized metadata; never publish credentials or raw data archives.
2. Stop the old application and prevent automatic or parallel restart. Verify it has actually exited and that no other scheduler/worker uses the same data directory, Spotify OAuth or existing destinations. An old `0.2.3b1` runtime may not emit the **new** `runtime_shutdown_incomplete` event. Do not rely on a missing event as proof of quiescence.
3. If shutdown occurred during an uncertain Spotify PUT/POST or a scheduler timeout, follow **Incident A** below before allowing a new instance to write. The new runtime's stop guards cannot revoke a request already sent by the old instance.
4. **No backup will be taken by owner choice.** A full consistent `/data` backup would be safer but is explicitly declined for this cutover. Record the choice as accepted irreversible loss risk; do not substitute a misleading portable ZIP/JSON or claim a guaranteed recovery path. If available without exporting private `/data`, review the four existing Spotify destination URLs/IDs, source identities and settings already known to the operator; this is identification, **not** a backup.
5. **The owner also declines the copy-based migration rehearsal** for this cutover. The synthetic v6–v10 tests and offline authority-first interruption probe remain the only migration evidence. Record that the actual `/data` has never been migrated in a rehearsal; check retained IDs, sources, policies, assets and journals read-only **after** the candidate first boots. Do not use production Spotify destinations as disposable test fixtures.
6. Freeze a reviewed code SHA **and** an independently verified image digest. Require a separate explicit authorization to publish or deploy. Do not point `edge`, `stable` or `latest` at the candidate merely to test migration.

**First-boot Spotify write boundary:** with saved authorization and at least one enabled playlist, `serve()` starts the scheduler as soon as runtime initialization succeeds. Its **first cycle runs immediately** and may write to the four existing Spotify destinations **before** any human can inspect the migrated settings through Admin. The current runtime has no documented read-only/dry-run startup mode that delays this first cycle. A successful post-boot inspection cannot retroactively authorize or prevent writes already sent. Consequently, before starting the candidate, the operator must **separately and explicitly approve this potential first-cycle Spotify activity**, or first implement and independently validate a provider-write containment mechanism. The decision to waive backups does **not** authorize immediate provider writes.

**Hard stop conditions remain:** old process not quiescent, unverified candidate identity, unapproved release/deployment, missing approval/containment for first-boot Spotify writes, or a known unresolved remote write that could collide with startup. **Declined backup and private rehearsal are explicitly accepted risks, not automatic blockers.** They reduce recovery options and do not turn a failed migration into a safe rollback.

## Incident A — ambiguous or late Spotify mutation

**Triggers:** mutating POST/PUT timed out, returned an uncertain 5xx/transport outcome, lost its acknowledgement, or remained in flight during container shutdown. A readback showing a desired state at one instant does **not** prove an older queued request cannot apply afterward.

1. Stop/pause automatic playlist mutations and prevent another engine instance or automatic restart from sending a newer write for the affected destination. Do not retry, rewrite the playlist or change its desired configuration merely to clear the symptom.
2. Preserve local journal, snapshot, cycle and shutdown diagnostics, the relevant commit/image identity and sanitized timestamps/event classes. Do **not** paste tokens, authorization headers, raw authenticated API responses, `/data`, or full private playlist contents into issues.
3. Record which destination and operation were affected, whether a request was actually sent, what was acknowledged, and which read-only observations exist. Distinguish an operation blocked **before** sending from one sent with unknown outcome.
4. Inspect the remote destination **read-only** using authorized operator access, preferably at more than one observation time. This can expose drift but cannot, by itself, establish that a delayed old request is impossible.
5. **Keep the affected destination paused from automated writes** until the prior request is definitively resolved by provider-authoritative evidence or a specifically verified containment strategy prevents collision with any new mutation. A general operator acceptance of residual risk is **not** resolution of a **known** outstanding remote write. The project currently has **no distributed fencing or provider-guaranteed upper bound for a previously sent write**. Repeated readbacks, a fixed waiting interval and a longer HTTP timeout cannot prove cancellation. If resolution/containment is unavailable, ordinary mutating operation remains **NO-GO**.
6. **Only after verified resolution or containment and explicit operator approval**, resume one controlled instance and observe the affected destination. Ensure its first scheduler cycle cannot write to an unresolved destination. Stop on unexplained drift. Never silently overwrite a confirmed newer state as part of automatic recovery.

**Important:** the offline test in [#421](https://github.com/eXPerience83/news-bulletin-playlist/issues/421#issuecomment-6656470956) demonstrates PUT A delayed → a new instance verifies B → old A arrives last. This is a valid modeled failure ordering, **not** evidence that Spotify commonly behaves that way.

## Incident B — v11 authority exists but v11 document is missing

**Trigger:** startup fails closed with `v11 authority exists but its document is missing`, or other conflicting authority/document evidence.

1. Keep the affected instance stopped; disable automatic restart while investigating. Never assume the root is fresh because the JSON is missing.
2. Without creating a private copy (the owner's choice), **leave the current `/data` unmodified** and record read-only metadata: presence and type of `.managed-state-v11.authority`, `managed-state-v11.json`, `managed-state.json`, SQLite/WAL, journals and assets, along with sanitized errors. Do not publish file contents, credentials or private root listings. No pre-cutover backup is available.
3. Confirm whether the missing-document symptom occurred at the known boundary: `V11OperationalStore.initialize_from_legacy()` publishes `.managed-state-v11.authority` *before* publishing `managed-state-v11.json`. The legacy JSON may still be byte-identical; this does **not** mean deleting the witness and retrying is supported.
4. Compare what can still be verified read-only on the original root with operator-known configuration and the remaining Spotify destinations. Investigate concurrent access and any existing v11 document. **There is no independent pre-cutover copy to compare or restore**, so exact lossless reconstruction may be impossible.
5. Choose recovery **only after review** of the remaining evidence. With no backup, the realistic fallback may be **manual reconstruction** rather than restoration, potentially creating new destinations and losing previous identity/history. This must be an explicit separate operator action; no automatic marker deletion, schema edits, blind retry, or old-image switch should run on the original root. Recreating local state does not undo any external Spotify write.
6. Document the approved recovery, exact state/image identities and fresh checks before any supervised reattempt.

The fail-closed error is intentional: a root with v11 ownership evidence must not silently fall back to its potentially stale legacy document.

## Incident C — candidate boot succeeds but acceptance differs

A healthy HTTP status does not verify state correctness. **If Spotify authorization is available and any playlist is enabled, the scheduler can already have written to production in its first immediate cycle**; after-boot checks are detection only, not a no-write preflight. With explicit approval or verified prior write containment, check: four unchanged production destination IDs, source/template order and assignments, duration/cap/retention settings, chosen covers and bytes, attribution, pending journals, SQLite continuity and diagnostics. Do not recreate production Spotify playlists to “repair” missing local references.

For a live provider acceptance, use **explicitly approved disposable LAB destinations**; the public destinations remain protected. Validate cover persistence after restart, first-attribution/cover behavior, partial RNE catalogue recovery, and controlled scheduler stop/restart separately. Unexpected mutation, missing asset or ambiguous provenance returns the operation to a **no-go** decision.

## Release gate and sign-off

Record **GO / NO-GO** for: immutable image, confirmed old-process stop, verified resolution/containment of any known uncertain remote write, explicit approval or verified containment for **immediate first-boot Spotify mutation**, schema/document evidence, known destination identities, post-boot cover/attribution checks and separately authorized TrueNAS acceptance. Record **backup: waived by owner** and **private rehearsal: waived by owner** as accepted data-loss risks, never as successful checks. A known unresolved cross-restart remote mutation is still **NO-GO** even when CI and CodeRabbit are green.

This runbook is deliberately conservative. It is not a promise of automatic repair, an instruction to expose production data, or a replacement for a separately authorized operator recovery.
