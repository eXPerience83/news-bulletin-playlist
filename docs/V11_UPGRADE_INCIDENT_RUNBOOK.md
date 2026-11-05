# v11 upgrade and Spotify-write uncertainty: operator incident runbook

**Status:** predeployment draft for review, not a production recovery authorization. Applies to a candidate upgrading an existing `0.2.3b1` (v6–v10) installation to forward-only v11. See [beta acceptance](BETA_V11_ACCEPTANCE.md), [TrueNAS deployment](DEPLOY_TRUENAS.md), [#565](https://github.com/eXPerience83/news-bulletin-playlist/issues/565) and [#421](https://github.com/eXPerience83/news-bulletin-playlist/issues/421).

**No automatic rollback.** Neither an old container image nor portable source JSON / playlist ZIP can restore a v11 installation. The four existing Spotify destinations must not be used as destructive test fixtures. This document provides decisions and evidence to collect; it does not claim that Spotify can cancel a previously transmitted mutation.

## Gate 0 — before authorizing the first candidate boot

1. Record the **actually running** old image ID/digest, application build, effective TrueNAS configuration and runtime stop/restart policy. The source repository's Dockerfile and YAML do not prove what TrueNAS is running. Capture only sanitized metadata; never publish credentials or raw data archives.
2. Stop the old application and prevent automatic or parallel restart. Verify it has actually exited and that no other scheduler/worker uses the same data directory, Spotify OAuth or existing destinations. An old `0.2.3b1` runtime may not emit the **new** `runtime_shutdown_incomplete` event. Do not rely on a missing event as proof of quiescence.
3. If shutdown occurred during an uncertain Spotify PUT/POST or a scheduler timeout, follow **Incident A** below before allowing a new instance to write. The new runtime's stop guards cannot revoke a request already sent by the old instance.
4. Take a **consistent independent backup of the complete `/data`**, including `managed-state.json`, SQLite plus relevant WAL/SHM, OAuth/credentials, provisioning and reconciliation journals, custom covers, assets, filesystem permissions/ACLs and other root evidence. Do not copy a live-changing SQLite database and call it consistent. Preserve a private original and validate a separate restored copy. A portable ZIP/JSON is not a full backup.
5. Rehearse the v11 conversion **only on an isolated restored copy** with provider network and real Spotify mutation disabled. Compare file inventory/hashes/metadata, frozen definitions, all four destination IDs, source selections, policies, drafts, cover bytes, pending states and SQLite evidence. Include the authority-first interruption case from the [independent audit](https://github.com/eXPerience83/news-bulletin-playlist/issues/565#issuecomment-6656470504).
6. Freeze a reviewed code SHA **and** an independently verified image digest. Require a separate explicit authorization to publish or deploy. Do not point `edge`, `stable` or `latest` at the candidate merely to test migration.

If any gate is missing, **do not start the candidate against production `/data`**.

## Incident A — ambiguous or late Spotify mutation

**Triggers:** mutating POST/PUT timed out, returned an uncertain 5xx/transport outcome, lost its acknowledgement, or remained in flight during container shutdown. A readback showing a desired state at one instant does **not** prove an older queued request cannot apply afterward.

1. Stop/pause automatic playlist mutations and prevent another engine instance or automatic restart from sending a newer write for the affected destination. Do not retry, rewrite the playlist or change its desired configuration merely to clear the symptom.
2. Preserve local journal, snapshot, cycle and shutdown diagnostics, the relevant commit/image identity and sanitized timestamps/event classes. Do **not** paste tokens, authorization headers, raw authenticated API responses, `/data`, or full private playlist contents into issues.
3. Record which destination and operation were affected, whether a request was actually sent, what was acknowledged, and which read-only observations exist. Distinguish an operation blocked **before** sending from one sent with unknown outcome.
4. Inspect the remote destination **read-only** using authorized operator access, preferably at more than one observation time. This can expose drift but cannot, by itself, establish that a delayed old request is impossible.
5. Escalate to an operator decision: keep the destination paused, obtain stronger provider-side evidence if available, or accept the documented residual risk before supervised resumption. The project currently has **no distributed fencing or provider-guaranteed upper bound for a previously sent write**. No fixed waiting interval and no extra HTTP timeout can prove cancellation.
6. After an explicit decision, restart only one controlled instance, observe the affected destination and stop on unexplained drift. Never silently overwrite a confirmed newer state as part of an automatic recovery.

**Important:** the offline test in [#421](https://github.com/eXPerience83/news-bulletin-playlist/issues/421#issuecomment-6656470956) demonstrates PUT A delayed → a new instance verifies B → old A arrives last. This is a valid modeled failure ordering, **not** evidence that Spotify commonly behaves that way.

## Incident B — v11 authority exists but v11 document is missing

**Trigger:** startup fails closed with `v11 authority exists but its document is missing`, or other conflicting authority/document evidence.

1. Keep the affected instance stopped; disable automatic restart while investigating. Never assume the root is fresh because the JSON is missing.
2. Preserve a complete **read-only forensic copy** of the current `/data` before attempting recovery, including `.managed-state-v11.authority`, `managed-state-v11.json` if present, `managed-state.json`, temporary files, SQLite/WAL, journals, assets and metadata. Record exact file presence, types and hashes privately. Preserve the original pre-cutover backup separately.
3. Confirm whether the missing-document symptom occurred at the known boundary: `V11OperationalStore.initialize_from_legacy()` publishes `.managed-state-v11.authority` *before* publishing `managed-state-v11.json`. The legacy JSON may still be byte-identical; this does **not** mean deleting the witness and retrying is supported.
4. Compare the private pre-cutover backup, the attempted new root and the independently rehearsed copy. Investigate whether another process accessed the root and whether an already-published v11 document or a partially committed action could exist elsewhere.
5. Choose recovery **only after review** of that evidence. A restore from the verified backup to a separate, isolated environment may be evaluated, but restoring local bytes cannot undo any external Spotify write. Do not edit schema versions, delete authority markers or switch to an older image on the original root as a generic repair.
6. Document the approved recovery, exact state/image identities and fresh checks before any supervised reattempt.

The fail-closed error is intentional: a root with v11 ownership evidence must not silently fall back to its potentially stale legacy document.

## Incident C — candidate boot succeeds but acceptance differs

A healthy HTTP status does not verify state correctness. Keep the candidate isolated and check: four unchanged production destination IDs, source/template order and assignments, duration/cap/retention settings, chosen covers and bytes, attribution, pending journals, SQLite continuity and diagnostics. Do not recreate production Spotify playlists to “repair” missing local references.

For a live provider acceptance, use **explicitly approved disposable LAB destinations**; the public destinations remain protected. Validate cover persistence after restart, first-attribution/cover behavior, partial RNE catalogue recovery, and controlled scheduler stop/restart separately. Unexpected mutation, missing asset or ambiguous provenance returns the operation to a **no-go** decision.

## Release gate and sign-off

Record **GO / NO-GO** for each of: exact immutable image, stopped old instance, absence/resolution of uncertain writes, complete private backup and test restore, isolated migration rehearsal, schema/document evidence, four destination identities, cover/attribution verification and authorized TrueNAS acceptance. Any unresolved ambiguity means **NO-GO**, even when Python CI, Container smoke and CodeRabbit are green.

This runbook is deliberately conservative. It is not a promise of automatic repair, an instruction to expose production data, or a replacement for a separately authorized operator recovery.
