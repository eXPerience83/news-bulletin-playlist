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

For `MATCHED` recovery, the temporal rejection is now anchored to `MatchedRecoveryState.first_attempt_at`, not the mutable scheduling timestamp `last_attempt_at`:

```text
match.updated_at > matched_recovery.first_attempt_at
```

`record_matched_recovery()` already preserves `first_attempt_at` for the same input fingerprint while later inconclusive attempts advance only `last_attempt_at` and `next_retry_at`. Therefore:

- a rejected repair remains a destination-safety barrier across later inconclusive retries;
- an inconclusive retry may move the next retry deadline without erasing the original rejected-transition evidence;
- restart preserves the same inference from durable SQLite state;
- a later shared catalogue remains shadow-only for A while A has no explicit intent;
- successful authoritative resolution still clears matched recovery through the normal observer path;
- no schema migration and no extra Spotify request are required.

The dedicated regression `test_inconclusive_due_retry_keeps_rejected_barrier_across_restart` covers:

1. durable `MATCHED(A)` at 14:00;
2. rejected A→B repair after rollback to 10:00;
3. matched recovery due at 16:00;
4. explicit 16:00 retry with a truncated two-page catalogue that is inconclusive;
5. scheduling advancing to 22:00 while `first_attempt_at` remains 10:00;
6. process restart;
7. a different native asset causing same-show `NEW_MATCH` at 16:10;
8. A receiving shadow contradiction from the shared complete catalogue;
9. no `match_input`, unchanged recovery/deadline, durable A preserved, and **zero PUT in two destinations**.

## Validation

The D1 one-shot validation generated functional head `f643ed41ea4805505f1a4f06a233329aec2d6205` with CPython 3.14.7:

- focused PR #211 persistence-rejection suite: **8 passed**;
- Ruff: pass;
- Ruff format: pass (**169 files already formatted** before temporary files were removed);
- mypy: pass (**55 source files**);
- `git diff --check`: pass;
- full pytest suite: **894 passed in 28.10s**.

All temporary apply/workflow files were removed before the functional commit. The net diff from `b9356e1` to `f643ed4` contains only `src/news_bulletin_playlist/engine.py` and `tests/test_pr211_persistence_rejection_barriers.py`. A user-authored documentation commit follows solely to trigger the normal PR CI and Container workflows on the exact final tree; those normal checks must still be green before the PR can leave draft.
