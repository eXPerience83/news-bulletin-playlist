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

The source-level duration policy is included in the matching source-contract fingerprint so existing planner evidence cannot silently survive a matching-rule change.

## Expected operational effects

- the #209 cached-ambiguity loop enters the existing six-hour matched-recovery backoff instead of polling every scheduler cycle;
- the observed Onda 309s/251s duplicate can resolve deterministically when Spotify durations match the production-established contract;
- resolving the 251s sibling also removes one current `AMBIGUOUS` retry contributor from #174;
- #148 complete/incomplete catalogue absence semantics remain unchanged;
- #181 remains fail-closed until exact qualifying matching-input evidence exists.
