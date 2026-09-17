# Title-format recovery contract

Title recovery exists to keep a bulletin source working through harmless upstream title-format drift
without broadening the identity of the configured news product. It is deterministic, local-only and
fail-closed.

## Parse stages

Every recovery-capable title parser uses the same ordered stages:

1. **exact** — run the provider's strict historical parser first;
2. **normalized** — retry the same strict parser after the reviewed generic syntax normalization;
3. **provider_compat** — for providers with an explicit profile, canonicalize a reviewed family of
   layout variants and require the strict parser to validate the resulting candidate;
4. **rejected** — no safe interpretation exists.

The reported mode is the stage that accepted the title. A `provider_compat` result may have passed
through generic normalization before the provider profile. The original RSS title is always retained
in `ParsedEdition` and `CanonicalEdition`; normalized/canonicalized strings are parse-only.

## Generic Unicode normalization

Generic recovery deliberately uses **NFC**, not NFKC. It may:

- compose canonically equivalent Unicode;
- collapse Unicode whitespace;
- remove the reviewed zero-width characters U+200B, U+200C, U+200D and U+FEFF;
- map the reviewed dash/minus variants to ASCII `-`.

It intentionally does **not** compatibility-fold full-width letters/digits, superscripts or other
Unicode compatibility characters into product/date tokens. Provider date/time grammars also require
ASCII digits (`0`-`9`) rather than Python's broader Unicode `\\d` class. A visually similar product
name or numeric token must not become valid merely because Unicode normalization or regex character
classes can transform/accept it.

## Provider compatibility profiles

RNE, SER and Onda Cero have explicit compatibility profiles. Profiles may tolerate reviewed syntax
families such as separators, terminal punctuation, hour suffix spacing and 2/4-digit years, but they
must preserve exact product identity and delegate final calendar/clock validation to the strict
parser.

Two-digit-year policy is intentionally provider-specific:

- RNE/SER/Onda Cero compatibility uses `20YY`, matching the bulletin-era RNE policy;
- CNN keeps its historical Python-style pivot used by `expand_two_digit_year`
  (`00..68 -> 2000..2068`, `69..99 -> 1969..1999`);
- COPE and RFI require four-digit years.

These policies are covered explicitly rather than hidden behind one global pivot.

## Bounded input

Recovery rejects titles longer than `MAX_TITLE_PARSE_CHARS = 1024` **before** the strict regex runs.
The limit is a defensive CPU boundary, not a semantic title-length rule: current bulletin grammars
and known provider examples are short human-readable titles, while 1024 leaves a deliberately large
margin for harmless presentation changes. Boundary and oversized behavior are covered in CI, and all
strict providers share the same pre-regex guard.

## Runtime drift guard

Runtime mode counters use a single `parse_with_recovery()` attempt per title. The denominator is the
number of items that actually reach title parsing, after required-field, collection-filter,
duplicate-native-id and publication-timestamp validation. It is not the raw RSS item count.

The fail-closed material-drift guard currently applies only to the dedicated bulletin parsers
`rne`, `ser` and `ondacero`:

- minimum parser sample: 3 titles;
- healthy boundary: at least 50% accepted;
- degradation: strictly below 50% accepted.

When that threshold is crossed after recovery, the fresh partial batch is discarded and the existing
last-known-good engine behavior remains authoritative. Mixed-programme sources such as RFI are not
subjected to this dedicated-feed threshold merely because non-target programmes are rejected.

## Provider watch

Provider-watch evaluates the same recovery contract. Its title sample is capped at six and uses the
same 50% acceptance boundary. Onda Cero's HTML fallback extracts complete outer title units, keeps
nested formatting inside that unit, and passes the complete title through recovery. It never treats a
valid-looking nested substring as an independent bulletin.

The fallback's lightweight Onda Cero prefilter uses the same safe syntax normalization so harmless
Unicode whitespace does not prevent a valid complete title from reaching the parser.

## Diagnostics and request budget

Recovery and runtime drift detection add:

- **0 RSS requests**;
- **0 Spotify Web API requests**;
- **0 OAuth requests**.

Production diagnostics expose only bounded aggregate counts for exact, normalized,
provider-compatible and rejected outcomes plus unresolved material drift. They do not add raw titles,
native IDs, fingerprints, URLs or provider payloads.

This contract does not modify Spotify matching, #148 pre-write catalogue safety, retention,
persistence barriers or Onda Cero duration disambiguation.
