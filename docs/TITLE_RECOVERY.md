# Title-format recovery contract

Title recovery keeps semantic edition-time parsing resilient to harmless upstream title-format drift
without broadening the identity of the configured news product. It is deterministic, local-only and
fail-closed.

The **current production runtime applies these parsers to episode titles returned by the official
Spotify show catalogue**. Spotify episode ID/URI remains identity. A parser result supplies optional
semantic `edition_at` metadata only; it never creates or changes episode identity.

## Parse stages

Every recovery-capable title parser uses the same ordered stages:

1. **exact** — run the provider's strict historical parser first;
2. **normalized** — retry the same strict parser after the reviewed generic syntax normalization;
3. **provider_compat** — for providers with an explicit profile, canonicalize a reviewed family of
   layout variants and require the strict parser to validate the resulting candidate;
4. **rejected** — no safe semantic interpretation exists.

The reported recovery mode is the stage that accepted the title. A `provider_compat` result may have
passed through generic normalization before the provider profile. The original Spotify catalogue
title remains stored as episode metadata; normalized/canonicalized strings are parse-only.

## Generic Unicode normalization

Generic recovery deliberately uses **NFC**, not NFKC. It may:

- compose canonically equivalent Unicode;
- collapse Unicode whitespace;
- remove the reviewed zero-width characters U+200B, U+200C, U+200D and U+FEFF;
- map the reviewed dash/minus variants to ASCII `-`.

It intentionally does **not** compatibility-fold full-width letters/digits, superscripts or other
Unicode compatibility characters into product/date tokens. Provider date/time grammars also require
ASCII digits (`0`-`9`) rather than Python's broader Unicode `\d` class. A visually similar
product name or numeric token must not become valid merely because Unicode normalization or regex
character classes can transform/accept it.

Unreviewed compatibility characters stay fail-closed. For example, soft hyphen U+00AD, word joiner
U+2060 and Unicode division/fraction slashes are not silently mapped into ordinary product/date
syntax. They require separate provider evidence before becoming an allowed recovery family.

Python's Unicode case-insensitive regex engine also treats a small set of non-ASCII letters as ASCII
I/i/S/K equivalents (U+0130, U+0131, U+017F and U+212A). Recovery rejects those characters,
including canonical decompositions that NFC would compose into one of them, before the historical
strict regex runs. The guard is repeated after the complete reviewed normalization chain and the
final candidate is NFC-composed again, so removing an allowed zero-width character cannot create a
new confusable only after the first check.

## Provider compatibility profiles

RNE, SER and Onda Cero have explicit compatibility profiles. Profiles may tolerate reviewed syntax
families such as separators, terminal punctuation, hour suffix spacing and 2/4-digit years, but they
must preserve exact product identity and delegate final calendar/clock validation to the strict
parser.

Compatibility profiles also preserve token boundaries: a time and date cannot be concatenated into
one undelimited numeric string. SER/Onda bracket punctuation, when present, must be balanced as
`(...)` or `[...]`; mismatched or one-sided brackets are rejected rather than normalized away.

Two-digit-year policy is intentionally provider-specific:

- RNE/SER/Onda Cero compatibility uses `20YY`, matching the bulletin-era RNE policy;
- CNN keeps its historical Python-style pivot used by `expand_two_digit_year`
  (`00..68 -> 2000..2068`, `69..99 -> 1969..1999`);
- COPE and RFI require four-digit years.

These policies are covered explicitly rather than hidden behind one global pivot. RNE's historical
compact time forms use HMM/HHMM semantics for every three- or four-digit token: the last two digits
are always minutes. Leading zeros therefore do not change `0015` into `15:00`; it is equivalent
to `00.15`.

## Bounded input

Recovery rejects titles longer than `MAX_TITLE_PARSE_CHARS = 1024` **before** the strict regex runs.
The limit is a defensive CPU boundary, not a semantic title-length rule: current bulletin grammars
and known provider examples are short human-readable titles, while 1024 leaves a deliberately large
margin for harmless presentation changes. Boundary and oversized behavior are covered in CI, and all
strict providers share the same pre-regex guard.

## Current Spotify-first runtime behavior

`fetch_source_catalogue()` reads episode metadata from official Spotify `show_episodes` pages. For
each valid catalogue episode that passes any configured collection filter:

1. the provider title parser receives the Spotify episode `name`;
2. a successful parse supplies semantic `edition_at` and records timestamp provenance as `title`;
3. an unsuccessful parse **does not discard the Spotify episode** merely because the title format is
   unknown;
4. instead, `edition_at` remains absent, timestamp provenance becomes `first_seen`, and the
   source's bounded `parser_fallback_count` is incremented;
5. Spotify `release_date` plus its explicit precision remain stored so local retention/order logic
   never invents minute precision from day/month/year-only provider metadata.

This preserves the Spotify-first authority split: title parsing improves editorial ordering when it
is trustworthy, while official Spotify episode identity and catalogue metadata remain authoritative
for the episode itself.

## Provider-watch tooling

Provider-watch remains a supported **tooling/CI** path for checking known provider title contracts.
It is not the production playlist ingestion path and it does not make RSS authoritative again.

Where provider-watch uses provider RSS or HTML to obtain title samples, those requests exist only to
detect parser/product drift before publication or during explicit investigation. The production
engine still detects source changes from public Spotify state and refreshes due sources from the
official Spotify Web API catalogue.

Provider-watch failure reports expose only bounded error classes/status codes; upstream exception
strings, URLs and arbitrary provider payload text are not copied into incident reports.

## Diagnostics and request budget

Title parsing and recovery are local computation. They add:

- **0 public Spotify detector requests**;
- **0 Spotify Web API requests**;
- **0 OAuth requests**.

Production source diagnostics expose a bounded `parser_fallback_count` alongside catalogue/filter
evidence. They do not add raw titles, episode IDs/URIs, URLs or provider payloads merely for parser
debugging.

This contract does not change detector identity, official catalogue authority, playlist retention,
destination reconciliation/readback, pending-snapshot safety or the global Spotify 429 cooldown.
