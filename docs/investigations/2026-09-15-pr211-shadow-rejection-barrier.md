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

## Validation

The one-shot validation that generated functional head `42770dcb46a1c1d9439289772987bd010818972b` completed with CPython 3.14.7:

- new shared-catalogue regression: pass;
- all PR #211 persistence-rejection regressions: pass;
- A1–A5/B1–B2 focused regressions: pass;
- Ruff: pass;
- Ruff format: pass;
- mypy: pass;
- full pytest suite: **891 passed in 28.05s**.

The temporary validation workflow and apply script were removed before the functional commit. A subsequent user-authored documentation commit is used only to let the normal CI and Container workflows validate the exact final PR tree.
