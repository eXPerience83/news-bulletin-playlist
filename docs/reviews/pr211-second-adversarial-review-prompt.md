# PR #211 — second adversarial review prompt

Review **read-only** PR #211 of `eXPerience83/news-bulletin-playlist` after the four blockers from the first adversarial review were fixed by ChatGPT.

PR: https://github.com/eXPerience83/news-bulletin-playlist/pull/211

Exact HEAD to review: `52823620bec2d5bd2a7732243a3c8884b0071f55`

Base/main: `bac53d72381a6a725cd3ca9476f835fdd1c12d92`

First blocked HEAD for comparison: `592abf7c7aed934bbb0f63a9bbdf88da207ed98b`

Do **not** edit files, commit, push, mark ready, or merge. You may run tests and temporary experiments outside the working tree. Leave the working tree clean.

## Context

#209 is about a production Onda Cero `MATCHED` evidence-recovery loop that consumed two bounded `show_episodes` pages about every scheduler cycle (~10 min), blocking #181 soft-retention evidence. #210 fixed the incomplete-catalogue-absence variant but production exposed a second condition: a durable cached URI could remain present and semantically viable while another Spotify candidate also became viable for the same semantic edition.

Production read-only SQLite evidence found two source-native RSS assets for Onda's 03:00 edition on 2026-09-14:

- asset A: RSS duration 309s, durable MATCHED URI, Spotify duration 309s;
- asset B: same title and `edition_at`, RSS duration 251s, previously AMBIGUOUS between two Spotify candidates including A's URI.

Across 144 Onda durable matches with both durations known, RSS↔Spotify delta was <=1s in 144/144 cases (65 exact, 79 delta 1s). This is not global, so duration disambiguation is exact-contract Onda-only.

The full production investigation and the first adversarial review findings are documented at:

`docs/investigations/2026-09-15-ondacero-duplicate-semantic-edition.md`

Related issues:

- #209 https://github.com/eXPerience83/news-bulletin-playlist/issues/209
- #181 https://github.com/eXPerience83/news-bulletin-playlist/issues/181
- #174 https://github.com/eXPerience83/news-bulletin-playlist/issues/174
- #207 https://github.com/eXPerience83/news-bulletin-playlist/issues/207
- #148 https://github.com/eXPerience83/news-bulletin-playlist/issues/148
- #142 https://github.com/eXPerience83/news-bulletin-playlist/issues/142
- merged first fix #210 https://github.com/eXPerience83/news-bulletin-playlist/pull/210

## First review blockers to re-test

The first adversarial review of HEAD `592abf...` was `BLOQUEADO` with four reproducible blockers. Try to reproduce each one again on the new HEAD. Do not accept tests as proof without inspecting behavior.

### A1 — cached viable mapping lost during SEMANTIC_REEVALUATION

Old failure:

- durable `MATCHED -> A`;
- input/contract changed;
- catalogue contains A and another semantically viable B;
- fresh result is AMBIGUOUS;
- old implementation persisted AMBIGUOUS, removed A from desired state, and could write playlists without A.

Expected now:

- A remains durable MATCHED if A is still semantically viable but uniqueness is lost;
- planner qualification is inconclusive (`None`), not positive and not contradiction;
- no destination removes A for that reason;
- stale input evidence is retired and bounded matched-recovery scheduling is established when needed.

Inspect especially `match_source_editions()`, `_assess_cached_mapping()`, `qualification_overrides`, `EngineRunner._observe_matching_result()`, desired-state construction and pre-write behavior.

### A2 — non-opted source fingerprints changed unnecessarily

Old failure:

- duration policy key was serialized with `None` for every source;
- all source-contract fingerprints changed;
- could cause a fleet-wide revalidation burst.

Expected now:

- for sources with no duration policy, `source_contract_fingerprint()` must be byte-for-byte/hash-equivalent to the historical pre-#211 payload;
- Onda exact opt-in contract should change compared with legacy;
- changing Onda tolerance should invalidate Onda evidence;
- SER/RNE/CNN/Reuters/RMF/ABC/UN/etc should not change merely because the optional feature exists.

Try to compare current output with a local reproduction of the base/main algorithm.

### A3 — stale match-input fingerprint bypassed recovery backoff

Old failure:

- durable MATCHED A;
- a `spotify_planner_match_inputs` row exists but contains an old fingerprint;
- current input fingerprint differs;
- first inspection is inconclusive, e.g. cached URI absent from a truncated two-page catalogue;
- old engine saw that a match-input row existed and did not record matched recovery;
- every next cycle woke again as INPUT_CHANGED and consumed two pages.

Expected now:

- first inconclusive inspection happens immediately;
- stale match-input is cleared, but current valid match-input would not be cleared;
- matched recovery is recorded against the **current** fingerprint;
- +10/+20/+30 min do not fetch for that recovery;
- restart during backoff does not fetch early;
- at ~6h deadline it retries;
- a genuinely changed input/fingerprint during backoff wakes immediately.

Try to reproduce with SQLite state inspection, not only outcome assertions.

### A4 — known duration overwritten by provider `None`

Old failure:

- durable MATCHED(A, 60);
- explicit EVIDENCE_RECOVERY sees A as uniquely semantically valid but provider `duration_ms=None`;
- old code treated `None != 60` as metadata drift and persisted `(A, None)`;
- planner then repeatedly woke on missing duration.

Expected now:

- known duration 60 is preserved;
- qualification is inconclusive;
- bounded recovery/backoff applies;
- no second fetch before deadline;
- known drift `60 -> 61` during explicit EVIDENCE_RECOVERY still repairs to 61 using the already-fetched catalogue and does not require another fetch.

## New shared cached-mapping design to audit

The revised implementation intentionally replaced overlapping repair/qualification logic with one internal `_CachedMappingAssessment` produced by `_assess_cached_mapping()`.

Verify that this single classification is internally consistent for both `SEMANTIC_REEVALUATION` and `EVIDENCE_RECOVERY` and that `qualification_overrides` cannot hide a later contradiction or incorrectly certify evidence.

Expected classification:

1. cached URI not found in bounded/incomplete candidate set -> inconclusive, no repair;
2. cached URI present but fails semantic evaluation -> positive contradiction, repair allowed from already-paid catalogue;
3. cached URI still viable, but fresh result PENDING/AMBIGUOUS because uniqueness is lost -> inconclusive, preserve cached mapping;
4. fresh rules uniquely select different URI -> contradiction/replacement;
5. same URI + same known duration -> qualified;
6. same URI + new known duration -> inconclusive outside explicit recovery; explicit recovery may refresh;
7. same URI + provider duration unknown -> inconclusive, preserve known durable metadata.

Important question: if cached URI is positively contradicted but the best fresh state is PENDING or AMBIGUOUS, is persisting that unresolved state justified and still safe for playlist reconciliation? The first review considered it defensible because the old mapping itself is positively contradicted; re-evaluate this on the final code and pre-write flow.

## #148 stale-mapping safety

Confirm no regression:

- cached URI absent from truncated/incomplete bounded catalogue must never be invalidated solely by absence;
- complete, structurally valid, non-empty catalogue absence may retain existing authoritative stale behavior;
- pre-write must preserve destination when mapping presence cannot be established;
- no duration rule may turn incomplete absence into proof.

Try cases where `evaluation_intents` are SEMANTIC_REEVALUATION and EVIDENCE_RECOVERY separately.

## #181 soft-retention safety

Confirm:

- `spotify_planner_matched_recovery_state` is still non-authoritative;
- it never substitutes for `spotify_planner_match_inputs`;
- missing/current-invalid matching evidence still fails closed for DEFER;
- a current valid match-input row is not destroyed merely because a later optional audit is inconclusive;
- after genuinely conclusive recovery, current match-input can be restored for future cycles.

Look for any path where `None` qualification plus recovery accidentally becomes local skip/defer authority.

## #174 unresolved retry/coalescing

Confirm:

- non-due PENDING/AMBIGUOUS does not independently wake catalogue matching;
- it may coalesce into a catalogue already fetched for another legitimate reason;
- coalescing cannot create a reason that wakes the next cycle by itself;
- same show/context still shares one bounded fetch per cycle.

Try to find an indirect loop through state updates.

## #142 rate-limit/cooldown

Confirm no alternate client/retry path bypasses the global Spotify cooldown and no change increases catalogue depth.

## Onda duration policy

Verify exact fail-closed scope:

- source id `ondacero`;
- parser `ondacero`;
- Spotify show `0tjEexypyczHXW9vE3SU3P`;
- tolerance ±1s;
- only after normal title/time/date/release semantics yield multiple viable candidates;
- RSS duration must exist;
- every viable Spotify candidate duration must exist;
- exactly one candidate inside tolerance;
- zero or multiple candidates inside tolerance => remain AMBIGUOUS;
- no duration rejection of a sole semantic candidate;
- no effect on another Onda show or other source.

Re-test the production-shaped 309/251 duplicate.

## Request-budget audit

Try to falsify the intended cadence.

For stale match-input + new fingerprint + inconclusive truncated catalogue, expected sequence is conceptually:

- first due inspection: two pages;
- next +10/+20/+30 min: zero matching pages for that recovery;
- restart before deadline: still zero;
- ~6h: two pages;
- real input change during backoff: immediate evaluation.

Also test:

- viable cached URI + AMBIGUOUS;
- same URI known duration drift;
- same URI provider duration None;
- unresolved coalescing;
- catalogue audit interaction;
- new_match/input_changed sharing the same show catalogue.

Report any path that can perform `show_episodes` every scheduler cycle without genuinely new information.

## Tests and structure

Normal GitHub validation for exact HEAD `52823620bec2d5bd2a7732243a3c8884b0071f55` is currently green:

- CI #914 success;
- Container #766 success;
- Ruff pass;
- Ruff format pass;
- mypy pass;
- 884 pytest tests pass.

Do not treat green CI as proof. Review tests for whether they assert behavior rather than merely implementation.

Pay particular attention to:

- `tests/test_engine.py` new A1/A3/A4 integration tests;
- `tests/test_pr211_adversarial_regressions.py`;
- `tests/test_matched_evidence_recovery_repairs.py`;
- `tests/test_ondacero_duration_disambiguation.py`;
- `tests/test_shadow_planner_review.py`.

Look for missing adversarial cases, especially complete vs incomplete catalogue, current-vs-stale match-input, persistence failure boundaries, restart, clock rollback, and destination mutation.

## Architecture review

Do not request cosmetic refactors. But flag any concrete structural problem that could recreate this class of bug.

Assess:

- whether `_CachedMappingAssessment` now provides one source of truth or still duplicates meaningful semantics elsewhere;
- whether bool|None remains too error-prone and an enum should be a follow-up;
- whether candidate evaluations are recomputed unnecessarily and whether a safe follow-up could reuse them;
- whether matching authority, match-input evidence, matched-recovery scheduling, unresolved scheduling, and destination authority remain cleanly separated;
- whether any further request-budget optimization is obvious and safe.

Classify improvements as `REQUIRED BEFORE MERGE` or `FOLLOW-UP`.

## Method

At minimum:

1. confirm exact HEAD;
2. inspect diff from base `bac53d...` and changes from first blocked `592abf...`;
3. read first review comment on #211 and updated investigation doc;
4. inspect matcher, engine, planner, persistence, desired-state/pre-write paths and relevant tests;
5. run focused tests and temporary adversarial experiments if useful;
6. confirm working tree clean at end.

## Response format

Start with:

`VEREDICTO: APTO PARA SALIR DE DRAFT`

or

`VEREDICTO: BLOQUEADO`

Then include:

### A. Re-test A1–A4

For each: `FIXED` or `STILL REPRODUCIBLE`, evidence and any reproduction.

### B. New required findings

Only reproducible correctness/safety/quota/persistence defects. Give severity, file/function, scenario, impact, minimal fix and missing test.

### C. Invariants

Explicitly answer:

- #148 regression yes/no;
- #181 regression yes/no and whether recovery is still non-authoritative;
- #174 regression yes/no and any indirect polling path;
- #142 regression yes/no.

### D. Request budget

State whether any every-10-minute `show_episodes` loop remains for:

- cached viable + ambiguity;
- stale match-input + inconclusive result;
- duration 60 -> None;
- duration 60 -> 61;
- coalesced unresolved;
- any other path you find.

### E. Onda policy

Confirm whether exact-source ±1s duration disambiguation remains fail-closed and justified by the measured evidence without generalizing it.

### F. Follow-ups

Concrete improvements only; separate them from blockers.

### G. Final

End exactly with one of:

`Cambios requeridos antes de merge: ninguno.`

or

`Cambios requeridos antes de merge: sí.`

If blocked, enumerate only actual blockers. If no blockers, explicitly say the PR can leave draft, but **do not change the PR yourself**.
