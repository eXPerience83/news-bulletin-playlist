# Timezones and playlist ordering

News Bulletin Playlist orders bulletins by their **semantic edition time**, not by the clock text that happens to remain visible in a provider or Spotify title.

This distinction matters whenever a source publishes in a timezone different from the playlist audience.

## Core rule

A provider title expresses the bulletin's local wall-clock time in the source's configured timezone.

The configured `SourceDefinition.timezone` is authoritative when that title is parsed. The resulting edition timestamp is then normalized and compared as an absolute instant for retention and playlist chronology.

`edition_at_desc` is the only supported ordering contract. When exact TITLE/PUBLIC evidence produced an `edition_at`, that physical UTC instant is authoritative. When exact `edition_at` is unavailable, Spotify-first ordering keeps FIRST_SEEN/coarse evidence on one physical timeline: the official Spotify day/month/year value defines a source-local release period using the source IANA timezone and DST rules, while `first_seen_at` is the real observation upper bound. The ordering instant is the earlier of `first_seen_at` and the end of that physical release period.

That fallback bound is ordering-only. It must not create `edition_at`, upgrade timestamp provenance, change retention semantics or invent exact minute precision. In particular, code must never splice a catalogue calendar date onto only the clock-of-day from `first_seen_at`; those are independent evidence sources and can cross UTC/source-local midnight differently.

The visible episode title must therefore **not** be used on its own to decide whether an item is chronologically misplaced.

## Reference example: CNN 5 Cosas

CNN 5 Cosas uses `America/New_York` as its source timezone and titles episodes using a US-style date plus a local `am`/`pm` clock.

For example:

```text
CNN 5 cosas 09/02/26 6am
```

means:

- `09/02/26` is US `MM/DD/YY`, so the date is **September 2, 2026**, not February 9;
- `6am` means **06:00 in America/New_York**;
- on September 2, 2026, 06:00 in New York corresponds to **12:00 in Europe/Madrid**.

Therefore, in a Spain-oriented playlist it is correct for that CNN episode to appear alongside Spanish bulletins from 12:00, even though Spotify still displays `6am` in the original episode title.

A list such as this is therefore valid:

```text
13:00  Cadena SER
12:00  CNN 5 Cosas — title still says "6am"
12:00  Onda Cero
```

The apparent mismatch is a presentation artifact in the provider title, not an ordering bug.

## FIRST_SEEN and coarse Spotify dates

A fallback row without exact `edition_at` carries two independent chronology constraints:

- `first_seen_at`: the physical instant when the runtime first observed that Spotify episode;
- the Spotify release-date period: day/month/year calendar evidence interpreted in the configured source timezone.

For playlist ordering, the row uses the earlier physical upper bound. If the first observation is inside the source-local release period, the real `first_seen_at` remains the ordering instant. If an older episode is first discovered later during bootstrap or catalogue refresh, the end of its release period caps that later observation so the row cannot jump ahead merely because it was discovered late.

Equal or overlapping coarse evidence remains genuinely ambiguous. Stable source/episode identity only provides deterministic total ordering; it does not manufacture semantic chronology.

## Do not hard-code timezone offsets

The Madrid/New York difference is not permanently six hours. Daylight-saving transitions do not occur on the same dates in Europe and the United States, so the offset can temporarily differ.

Code and diagnostics must use the configured IANA timezone names, such as `America/New_York` and `Europe/Madrid`, rather than assumptions such as `CNN time + 6 hours`.

The same rule applies to coarse Spotify release periods: their physical UTC boundaries come from the source IANA timezone, including DST transitions, rather than from UTC midnight or a fixed offset.

## Troubleshooting a bulletin that looks out of order

Before changing parser or ordering code, check these points in order:

1. Confirm the provider-specific date format. A numeric date may not use `DD/MM/YYYY`.
2. Confirm the source timezone configured for that provider.
3. Confirm that the title/public parser produced the expected exact `edition_at`, when exact evidence exists.
4. Convert that instant to the timezone relevant to the playlist/audience when comparing it visually with local bulletins.
5. If no exact `edition_at` exists, inspect both the Spotify release-date value/precision and the real `first_seen_at`. Convert the source-local release-period end through the configured IANA timezone and verify that ordering uses `min(first_seen_at, release_period_end)` rather than grafting one evidence source's date onto another source's clock.
6. Confirm that FIRST_SEEN/coarse fallback did not create `edition_at`, upgrade provenance or change retention semantics.
7. Only treat the item as misplaced if the parsed semantic time/provenance is wrong or the Spotify-first ordering helper does not follow that evidence.

This check is especially important for international sources because Spotify normally preserves the provider's original title and does not rewrite the displayed clock into the listener's local timezone.

## Regression principle

The CNN example above is a useful human-readable regression case. If `CNN 5 cosas 09/02/26 6am` appears around the 12:00 Spanish bulletins, that is expected behavior. A future implementation change should not "fix" this by sorting the literal `6am` as though it were 06:00 in Spain.

A second regression class is a FIRST_SEEN/coarse episode around source-local midnight. Its Spotify release day and its real observation instant may fall on different UTC dates. That episode must stay on one physical timeline; combining the release calendar date with only the observation clock-of-day can manufacture an instant that never existed and incorrectly place a fallback row ahead of later exact bulletins.

See also [`ARCHITECTURE.md`](ARCHITECTURE.md), especially the detector/catalogue, timestamp and desired-state contracts that define source timezone authority and semantic `edition_at` ordering.
