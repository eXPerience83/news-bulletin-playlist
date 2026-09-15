# Onda Cero duplicate semantic edition investigation — 2026-09-15

## Trigger

Production validation after #210 still showed `matching_reason_evidence_recovery_count=1` for Onda Cero with two `show_episodes` pages on roughly every scheduler cycle. The v6 matched-recovery backoff did not become durable.

The investigation used the production TrueNAS SQLite database in strict read-only mode. No Spotify writes, database mutations, or LAB playlist changes were performed.

## Exact durable state

Schema v6 was present. Onda Cero had one recent/relevant `MATCHED` edition without `spotify_planner_match_inputs` and no row in `spotify_planner_matched_recovery_state`:

- semantic edition: `2026-09-14T01:00:00Z` (`03:00` Europe/Madrid);
- RSS duration: `309s`;
- persisted Spotify duration: `309s`;
- original matcher result: one candidate out of the bounded 100-item catalogue.

A second source-native RSS asset later appeared with the same title and semantic `edition_at`, but duration `251s`. It remained `AMBIGUOUS` because the current Spotify catalogue exposed two semantically viable episode candidates for that edition, including the already-persisted URI of the 309s asset.

This was the only duplicated Onda Cero `(edition_at,title)` group in the database at investigation time.

## Root cause after #210

#210 correctly classified absence from a truncated bounded catalogue as inconclusive. It did not cover a different inconclusive state: the persisted URI can still be present and semantically viable while another candidate also becomes viable.

The shadow qualifier reevaluated the whole catalogue and required a unique result equal to the cached `(URI,duration)`. An `AMBIGUOUS` result therefore became `False`. Engine observation treats `False` as active contradiction and clears both match-input evidence and matched-recovery state. The next cycle again sees `MATCHED + missing match_input + no recovery state`, recreating the every-cycle recovery loop.

The same structural loop can occur when the persisted URI remains uniquely semantically valid but Spotify reports a changed duration: exact `(URI,duration)` qualification fails even though the URI itself is not contradicted.

## Duration evidence

For Onda Cero persisted matches with both RSS and Spotify duration available:

- samples: `144`;
- absolute delta `0s`: `65`;
- absolute delta `1s`: `79`;
- median: `1s`;
- p95: `1s`;
- maximum: `1s`.

The signal is not safe globally. The same production database showed materially larger deltas for CNN, Reuters and RMF, plus SER outliers. Duration disambiguation must therefore be source-scoped and disabled by default.

## Fix contract

The follow-up to #209 uses two independent safety rules:

1. **Tri-state qualification** — a cached URI that still passes semantic evaluation but cannot be selected uniquely is `None` (inconclusive), not `False` (contradicted). Exact metadata drift on the same uniquely valid URI is also inconclusive. A cached URI that fails semantic evaluation, or current matching rules uniquely select another URI, remains negative evidence.
2. **Onda-only duration disambiguation** — after title/time/date semantics produce multiple viable candidates, duration may select one candidate only when the source is explicitly opted in, the RSS duration is known, every viable Spotify candidate has duration, and exactly one candidate falls within the configured tolerance. Onda Cero uses `±1s`; all other sources remain disabled.

The source-level duration policy is included in the matching source-contract fingerprint so existing Onda planner evidence cannot silently survive a matching-rule change. Sources without a duration policy retain the exact legacy source-contract fingerprint payload; merely adding the optional feature must not invalidate their planner evidence.

## Adversarial review of the first #211 implementation

The first green #211 head (`592abf7c7aed934bbb0f63a9bbdf88da207ed98b`) was deliberately reviewed adversarially before leaving draft. Four reproducible blockers were found:

1. **Cached viable mapping could be lost during `SEMANTIC_REEVALUATION`.** The protection for `cached URI still viable + fresh result AMBIGUOUS` existed only on the evidence-recovery path. A contract/input change could therefore persist `AMBIGUOUS`, remove the cached URI from desired state, and let playlist reconciliation remove a still-defensible episode.
2. **Optional duration policy invalidated every source fingerprint.** The first implementation serialized a new key with `None` for non-opted sources, changing otherwise-identical hashes and potentially causing a fleet-wide revalidation burst.
3. **Old match-input evidence could defeat recovery backoff.** An existing `spotify_planner_match_inputs` row with a stale fingerprint was treated differently from an absent row after an inconclusive inspection. Because recovery was only recorded when the row was literally absent, the next cycle could wake again as `INPUT_CHANGED` and repeat the bounded catalogue read.
4. **Known duration could be overwritten by unknown provider metadata.** During explicit recovery, `60 -> None` was treated as metadata drift and could persist `MATCHED(uri, None)`, destroying known durable metadata and creating a new missing-evidence loop.

These findings did not require relaxing #148, #181, #174, or #142. They exposed missing consistency between cached-mapping qualification, repair, and planner scheduling.

## Corrected #211 design

The revised implementation uses one internal cached-mapping assessment for both `SEMANTIC_REEVALUATION` and `EVIDENCE_RECOVERY`. It classifies the already-fetched catalogue once and drives both production mutation and planner qualification from the same semantics:

- cached URI not present in a bounded/incomplete catalogue -> inconclusive, preserve mapping;
- cached URI present but semantically invalid -> positive contradiction; the already-paid catalogue may persist the best fresh state;
- cached URI still semantically viable but uniqueness is lost -> inconclusive, preserve mapping;
- current rules uniquely prefer another URI -> positive contradiction/replacement;
- same URI and same known duration -> qualified;
- same URI with a new **known** duration -> inconclusive outside explicit recovery, but explicit recovery may refresh it in the same paid inspection;
- same URI with provider duration now unknown -> inconclusive; preserve the known durable duration.

Engine observation also distinguishes a match-input row that merely exists from evidence valid for the current fingerprint. When a MATCHED result is inconclusive after an inspection and the stored input fingerprint is stale, the stale row is cleared and a non-authoritative matched-recovery deadline is recorded for the **current** fingerprint. A genuinely current match-input row is preserved.

`spotify_planner_matched_recovery_state` remains scheduling metadata only. It never substitutes for `spotify_planner_match_inputs` and cannot authorize #181 soft-retention.

## Regression coverage after the first adversarial findings

The revised tests cover the four blockers and their request-budget consequences, including:

- `SEMANTIC_REEVALUATION` with cached URI still viable plus another candidate preserves the durable MATCHED URI and playlist contents;
- non-opted sources keep the exact legacy source-contract fingerprint, while Onda policy/tolerance changes invalidate Onda as intended;
- stale match-input fingerprint + truncated absence performs the first two-page inspection, then no matching catalogue reads at +20/+30 minutes, survives EngineRunner restart, and retries at the six-hour deadline;
- a genuinely new input during matched-recovery backoff wakes evaluation immediately;
- known provider duration becoming unknown preserves the known duration and enters bounded recovery instead of polling;
- known duration drift (`60 -> 61`) remains repairable during explicit evidence recovery using the already-fetched catalogue;
- the production-shaped Onda 309s/251s duplicate still resolves deterministically under the exact Onda-only duration policy.

## Second adversarial review: persistence acknowledgement

A second independent adversarial review confirmed A1–A4 fixed, then found a consistency defect at the boundary between matching and durable persistence. `spotify_matches` intentionally rejects stale writes with the monotonic guard `excluded.updated_at >= spotify_matches.updated_at`. During runner clock rollback, explicit recovery could discover a conclusive replacement B, attempt to persist it with an older timestamp, have SQLite reject the UPDATE, and still return B as a fresh qualified outcome. Engine observation could then publish current `match_input` evidence even though SQLite still contained A. The review demonstrated that this false evidence could make #181 soft-retention return `DEFER` for A despite the catalogue having positively contradicted A.

The first persistence-boundary correction kept the temporal guard and made its result explicit:

- `SQLiteStore.set_match_state()` returns whether the INSERT/UPDATE actually applied;
- the cached-repair path publishes the fresh repaired outcome only when persistence acknowledges success;
- a rejected cached repair keeps the returned outcome aligned with the durable mapping and keeps qualification fail-closed;
- Engine cannot publish positive `match_input` evidence for that uncommitted repair.

## Third adversarial review: rejected-persistence barrier and bounded retry

A third independent review re-tested A1–A5 and found two remaining persistence-boundary defects:

1. **B1 — rejection was not propagated through every authoritative path or destination authorization.** Complete-catalogue absence and ordinary fresh-resolution paths could still ignore the guarded-write acknowledgement. A proposed state could therefore become planner evidence while SQLite retained the newer durable state. The review also demonstrated a destination write of contradicted A when a rejected repair left A durable and pre-write accepted its mere catalogue presence.
2. **B2 — a positively contradicted repair rejected by the temporal guard could poll every scheduler cycle.** The semantic qualification remained `False`, which correctly removed stale positive evidence, but also left no durable retry schedule. During a long clock rollback this recreated two bounded `show_episodes` pages every ~10 minutes until wall-clock time caught up.

The final correction treats persistence rejection as a separate, non-authoritative state boundary:

- every authoritative guarded match write consumes the `set_match_state()` acknowledgement before publishing a proposed state;
- rejected outcomes carry `persistence_rejected=True`, while the public outcome remains aligned with durable SQLite;
- Engine publishes no positive match-input evidence from a rejected write;
- the affected source is fail-closed for destination reconciliation in the same cycle;
- a durable rejection barrier is derived from the newer mapping plus its bounded matched-recovery/unresolved attempt and is checked before the pre-write catalogue-presence shortcut;
- a rejected `MATCHED` repair records matched-recovery for the current fingerprint; rejected non-MATCHED state records unresolved retry state;
- `CLOCK_ROLLBACK` is not promoted into an every-cycle matching wake-up for this barrier;
- the barrier and retry deadline survive EngineRunner restart;
- a quiet cycle after wall-clock time merely passes the durable mapping timestamp still performs no matching fetch: retry waits for the recorded recovery deadline unless genuinely new input/fingerprint work creates an independent valid wake-up reason;
- at the bounded retry deadline, the replacement may be applied and certified if the temporal guard now accepts it.

This preserves the ordering guard rather than defeating it with artificial timestamps. Recovery/unresolved rows remain scheduling metadata only and never authorize #181 by themselves.

## Final regression coverage for B1/B2

The final focused regressions cover the exact failure modes from the third review:

- complete-catalogue absence with an older observation cannot publish a proposed invalidation or current planner evidence when SQLite rejects it;
- a rejected repair with a physically divergent destination performs **zero PUT/replace** and preserves the destination;
- the same safety barrier applies whether the old URI is absent from a complete catalogue or present but positively semantically contradicted;
- #181 does not accept `DEFER` from rejected-persistence scheduling without current authoritative match-input evidence;
- repeated +10/+20/+30-minute scheduler cycles during rollback do not re-fetch the show;
- restart during the rejection backoff does not cause a premature fetch;
- merely advancing past the old mapping timestamp does not bypass the durable retry deadline;
- at the retry deadline, a now-acceptable replacement converges to B and only then publishes current evidence;
- ordinary fresh resolution also stays aligned with durable SQLite if its guarded write is rejected.

The one-shot validation that produced the functional B1/B2 correction completed successfully with CPython 3.14.7:

- focused adversarial regressions: pass;
- Ruff: pass;
- Ruff format: pass;
- mypy: pass;
- full pytest suite: **890 passed in 28.91s**.

The temporary one-shot workflow, patch, and apply script were removed before the functional commit and are not part of the PR diff.

## Expected operational effects

- the #209 cached-ambiguity loop enters the six-hour matched-recovery backoff instead of polling every scheduler cycle;
- an input/contract change that yields only inconclusive evidence does not discard a still-viable cached mapping;
- stale match-input rows cannot keep `INPUT_CHANGED` alive every ten minutes after an inconclusive inspection;
- non-opted sources avoid an unnecessary fingerprint-driven revalidation burst;
- unknown provider duration cannot erase known durable duration and create a new missing-evidence loop;
- a temporal SQLite no-op cannot be mistaken for a committed repair or become positive planner evidence;
- a temporally rejected contradiction cannot authorize a destination write and receives bounded durable scheduling instead of ten-minute polling;
- the observed Onda 309s/251s duplicate can resolve deterministically when Spotify durations match the production-established contract;
- resolving the 251s sibling also removes one current `AMBIGUOUS` retry contributor from #174;
- #148 complete/incomplete catalogue absence semantics remain unchanged;
- #181 remains fail-closed until exact qualifying matching-input evidence exists;
- #142 global Spotify cooldown remains authoritative.
