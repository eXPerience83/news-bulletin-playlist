# Timezones and playlist ordering

News Bulletin Playlist orders bulletins by their **semantic edition time**, not by the clock text that happens to remain visible in a provider or Spotify title.

This distinction matters whenever a source publishes in a timezone different from the playlist audience.

## Core rule

A provider title expresses the bulletin's local wall-clock time in the source's configured timezone.

The configured `SourceDefinition.timezone` is authoritative when that title is parsed. The resulting edition timestamp is then normalized and compared as an absolute instant for retention and playlist chronology.

For playlists configured with `edition_at_desc`, ordering uses `edition_at` and falls back to RSS `published_at` only when no reliable semantic edition timestamp exists.

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

## Do not hard-code timezone offsets

The Madrid/New York difference is not permanently six hours. Daylight-saving transitions do not occur on the same dates in Europe and the United States, so the offset can temporarily differ.

Code and diagnostics must use the configured IANA timezone names, such as `America/New_York` and `Europe/Madrid`, rather than assumptions such as `CNN time + 6 hours`.

## Troubleshooting a bulletin that looks out of order

Before changing parser or ordering code, check these points in order:

1. Confirm the provider-specific date format. A numeric date may not use `DD/MM/YYYY`.
2. Confirm the source timezone configured for that provider.
3. Confirm that the title parser produced the expected local `edition_at`.
4. Convert that instant to the timezone relevant to the playlist/audience when comparing it visually with local bulletins.
5. Confirm the playlist ordering policy (`edition_at_desc` versus `published_at_desc`).
6. Only treat the item as misplaced if its canonical timestamp is wrong or the desired-state ordering does not follow that timestamp.

This check is especially important for international sources because Spotify normally preserves the provider's original title and does not rewrite the displayed clock into the listener's local timezone.

## Regression principle

The CNN example above is a useful human-readable regression case. If `CNN 5 cosas 09/02/26 6am` appears around the 12:00 Spanish bulletins, that is expected behavior. A future implementation change should not "fix" this by sorting the literal `6am` as though it were 06:00 in Spain.

See also [`ARCHITECTURE.md`](ARCHITECTURE.md), especially the collection and desired-state contracts that define source timezone authority and semantic `edition_at` ordering.
