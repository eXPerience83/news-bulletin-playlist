# PR #211 shared-catalogue rejection barrier follow-up — 2026-09-15

## Adversarial finding

A read-only adversarial review of head `f455635aba5be57c4669becf8632eb2d6d2c6b95` confirmed the earlier A1–A5 and direct B1/B2 regressions were fixed, then found a residual interaction between a durable persistence-rejection barrier and a later shared-catalogue shadow evaluation.

Reproduction:

1. `MATCHED(A,60)` is durable at 14:00.
2. Runner time rolls back to 10:00 and an explicit recovery proves A semantically invalid while B is the unique replacement.
3. SQLite correctly rejects the older repair timestamp; Engine clears positive evidence, records matched recovery to 16:00, and preserves the destination.
4. At 10:10 another native asset from the same show legitimately wakes the shared catalogue through `NEW_MATCH` (the same interaction is possible with a catalogue audit).
5. A itself is not due and therefore has no explicit evaluation intent, but it receives shadow qualification from the already-paid catalogue.
6. The shadow result is contradictory (`planner_input_evidence_qualified=False`) while `persistence_rejected=False`, because no persistence attempt was made for A in this call.
7. The old observer treated that shadow contradiction as permission to clear matched recovery. That deleted the row underpinning the rejection barrier.
8. Pre-write could then accept A merely because A was physically present in the catalogue, permit `PUT(A)`, and the next quiet cycle could recreate premature `EVIDENCE_RECOVERY` instead of waiting until 16:00.

The review reproduced this with SQLite, a 51-item/two-page catalogue, one and two playlists, `NEW_MATCH`, and catalogue-audit wake-up.

## Correction

Engine observation now receives the explicit `evaluation_intents` that caused work for each edition and distinguishes explicit evaluation from shadow-only reuse of a catalogue fetched for another reason.

For an edition with an active persistence-rejection barrier:

- shadow contradiction may still clear stale positive `match_input` evidence;
- shadow contradiction does **not** clear the matched-recovery row that represents the unresolved rejected repair;
- shadow inconclusion does **not** re-record/rebase that recovery deadline;
- only an explicit evaluation of that edition may resolve or replace the barrier according to the normal persistence acknowledgement rules;
- pre-write continues to see the durable barrier and preserves the destination while it remains unresolved.

This keeps same-show catalogue sharing: no extra Spotify GET is introduced. The fix only prevents shadow observation from mutating scheduling authority that it did not explicitly evaluate.

## Regression coverage

`test_shared_catalogue_preserves_rejected_repair_barrier` reproduces the reported sequence:

- initial rejected repair at 10:00 creates recovery due at 16:00 and performs zero destination replacement;
- a legitimate same-show `NEW_MATCH` at 10:10 consumes the shared bounded catalogue but leaves A's durable mapping, absent match-input, recovery object, deadline, and destination unchanged;
- the following quiet cycle at 10:20 performs no premature matching fetch and no destination write.

The same focused suite retains the prior persistence-rejection, complete/incomplete catalogue, #181 fail-closed, restart, rollback and eventual-convergence regressions.

## D1 — an inconclusive explicit retry erased the barrier indirectly

A final Codex + ASTRA audit of head `b9356e14625111ac344444e59146fee5b47dba25` found one further interaction. The original temporal rejection was inferred with:

```text
match.updated_at > matched_recovery.last_attempt_at
```

That is sufficient only until the first explicit retry. In the reproduced sequence, durable A was stored at 14:00, the rejected repair was attempted at 10:00, and matched recovery was due at 16:00. At 16:00 the bounded catalogue was truncated and did not contain A, so the retry was correctly inconclusive and only advanced scheduling to 22:00. Updating `last_attempt_at` to 16:00 made the comparison `14:00 > 16:00` false even though no durable repair had been accepted.

After restart, a different same-show `NEW_MATCH` at 16:10 paid for a complete shared catalogue containing A as a semantically invalid candidate and B as the correct replacement. Because the barrier had disappeared, shadow contradiction could clear recovery and pre-write could then accept A through the physical-presence shortcut. The audit reproduced an incorrect replace containing A in two destinations.

### D1 correction

For `MATCHED` recovery, the temporal rejection was first anchored to `MatchedRecoveryState.first_attempt_at`, not the mutable scheduling timestamp `last_attempt_at`:

```text
match.updated_at > matched_recovery.first_attempt_at
```

That closes D1 only while the scheduling fingerprint remains unchanged. The dedicated regression `test_inconclusive_due_retry_keeps_rejected_barrier_across_restart` retains coverage for multiple inconclusive retries, restart, shared same-show work and zero PUT.

## D2 — fingerprint changes proved scheduling cannot own the safety barrier

Codex + ASTRA then audited exact head `c70d755fd1e151123bc5d8ee98e3c2be9e1f9370` and found D2. `record_matched_recovery()` correctly starts a new scheduling series when the matching-input fingerprint changes. That means `first_attempt_at` also changes, so D1's timestamp inference still coupled destination safety to scheduling state.

The reproduced sequence was:

1. durable `MATCHED(A,60)` updated at 14:00;
2. clock rollback to 10:00; explicit A→B repair rejected by SQLite; zero PUT;
3. matched recovery starts at 10:00 and is due at 16:00;
4. at 15:00 only RSS `pubDate` changes, producing a legitimate new input fingerprint;
5. `INPUT_CHANGED` immediately inspects the bounded catalogue, which is truncated and therefore inconclusive;
6. the recovery series is correctly reset for scheduling to `first_attempt_at=15:00`, `next_retry_at=21:00`;
7. timestamp inference therefore loses the rejection barrier even though no repair was persisted;
8. after restart, a sibling `NEW_MATCH` pays for a shared catalogue where A is still semantically invalid and B is correct;
9. A is shadow-only, recovery can be cleared, and pre-write can accept A by physical presence;
10. both destinations can incorrectly receive PUT containing A.

### D2 correction — schema v7 first-class barrier

D2 makes the architectural separation merge-critical. Schema v7 adds `spotify_planner_persistence_rejection_state`, keyed by `(source_id, source_native_id)` and deliberately independent of input fingerprints and retry deadlines.

The barrier stores an exact snapshot of the durable match that won the temporal race:

- protected match status;
- protected Spotify URI and duration;
- protected match `updated_at`;
- first and last rejection observation timestamps.

On every `persistence_rejected` outcome Engine records this barrier **before** changing retry scheduling. `match_input` remains non-authoritative and is cleared. `matched_recovery`/`unresolved` remain scheduling-only: an input change may legitimately reset their fingerprint and retry series without altering destination safety.

The barrier resolves only when there is sufficient authority:

- an explicit conclusive evaluation may clear it; or
- an accepted durable state transition changes the protected match snapshot, so the old barrier no longer protects the new durable row and is cleared lazily.

Shadow `False`, `True` or `None` never resolves the first-class barrier. This preserves same-show catalogue sharing without an extra Spotify request.

Migration v7 conservatively reconstructs barriers that can be proven from the previous v6 implicit state. The reconstruction runs transactionally only when the required canonical/planner tables exist; partial historical/synthetic databases remain upgradeable and simply have no reconstructable barrier.

### D2 regression coverage

`test_input_fingerprint_change_cannot_erase_rejected_repair_barrier` now covers the complete D2 reproduction:

1. rejected A→B repair under clock rollback;
2. two empty/unknown destinations and zero PUT;
3. `pubDate`-only input fingerprint change;
4. immediate `INPUT_CHANGED` inspection with a truncated 100/101 catalogue;
5. recovery scheduling reset to the new fingerprint while the first-class barrier remains unchanged;
6. restart;
7. sibling same-show `NEW_MATCH` with A contradicted and B correct;
8. A remaining shadow-only;
9. absent `match_input`, unchanged durable A and barrier, and zero PUT in both destinations;
10. later explicit due recovery accepting B, restoring current evidence, clearing recovery/barrier and reconciling both destinations normally.

Additional journal/migration regressions prove that a recovery fingerprint change does not mutate the barrier lifecycle, an accepted durable match no longer matches the protected snapshot, and a real schema-v6 implicit barrier is seeded into v7. The historical synthetic schema-v2 duration migration also remains green.

## Future publication after a second clock rollback

The post-D2 audit reproduced a remaining destination-safety escape on `cf84b711`:
durable A at 14:00, a rejected A-to-B repair at 10:00, then a restart at 08:50.
A's publication (09:05) and semantic edition time (09:00) were both in the future.
The source-level check excluded its still-active barrier, while empty desired state
bypassed the per-item prewrite checks. Both physical destinations were emptied.

Barrier enforcement now retains future-published editions within the existing lower
retention bound. Matching still uses its original publication window; this change
does not add matching work, change retry scheduling, or keep expired history blocking
destinations indefinitely.

`test_future_edition_rejection_barrier_preserves_destinations_after_restart` reproduces
the rejected two-page repair with real SQLite and two simulated destinations. It failed
before the fix. It verifies zero PUT after the second rollback/restart, unchanged durable
mapping/barrier/recovery, absent match-input evidence, and eventual convergence to B after
an accepted due repair. Catalogue reads remain shared and bounded to two pages per
inspection. The PR remains draft pending independent review of the new head.

## Historical D2 validation

D2 functional commit: `b0cf769b869fbba714d5d139a62d7329395ae710`.

One-shot validation with CPython 3.14.7:

- focused PR #211 persistence-rejection suite: **9 passed**;
- focused planner/persistence/duration migration suites: **29 passed**;
- Ruff: pass;
- Ruff format: pass (**168 files already formatted**);
- mypy: pass (**55 source files**);
- `git diff --check`: pass;
- full pytest suite: **897 passed in 26.71s**.

Normal GitHub validation is required on the exact documentation head that follows this record. The immediately preceding documentation head `6f4fb41bb0691fd810fd9b7f96d3c025c2c77d16` was green in CI #956 and Container #808 with **897 passed**, Ruff/format/mypy clean, and container build/live-provider/hardened-runtime/restart validation green. The current documentation-only successor must also be green before independent audit sign-off.

All temporary apply/workflow files were removed before the functional commit. The PR remains draft until a fresh independent audit of the exact current head is complete.
