# Spain / Latin America source separation and expansion

This document stages the post-#130 source-catalog follow-up. It does **not** change runtime behavior by itself.

## Product decision

Spanish-language products must separate **editorial market** and **spoken locale**.

A source is not eligible for a Spain playlist merely because it is published in Spanish, and a Latin-American Spanish source is not silently treated as equivalent to `es-ES`.

The target product matrix is:

| Product | Editorial target | Spoken locale | Intended role |
| --- | --- | --- | --- |
| `ES · es-ES` | Spain | `es-ES` | Spain-focused national/general news |
| `LAT · es-419` | Latin America / Americas | `es-419` | Latin-America-focused news |
| `INT · es-ES` | World | `es-ES` | International/world news spoken in Spain Spanish |
| `INT · es-419` | World | `es-419` | International/world news spoken in Latin-American Spanish |

`es` remains valid only for genuinely neutral/general Spanish or for a source whose spoken variant is not yet proven. An `es` source must **not** be automatically shared between Spain and LAT products.

For user-facing naming, prefer unambiguous concepts such as:

- **Noticias España**
- **Noticias Latinoamérica**
- **Mundo en Español · ES**
- **Mundo en Español · LAT**

The compact internal/product notation can evolve separately from cover wording, but it must preserve the same distinction.

## Consequence for PR #130 sources

PR #130 remains unchanged while its TrueNAS candidate is under acceptance. The follow-up PR after #130 merges should apply these source-placement rules:

- `ser`, `rne`, `ondacero`, `abc` remain strong defaults for `ES · es-ES`.
- `cnn` must no longer be shared with the Spain playlist merely because it is in Spanish. Its spoken locale should be verified explicitly; until then, treat its current `es` tag as unresolved for Spain/LAT placement.
- `RFI Español — Noticias de América` is regional-Americas content and belongs only in the LAT research bucket; it is not a default world-Spanish or Spain source.
- sources such as ONU, DW, France 24, Univision, SBS and VOA require both editorial-scope and spoken-locale verification before assignment.

## Spanish-language source backlog

### Spain / `ES · es-ES`

| Source | Scope | Status | Note |
| --- | --- | --- | --- |
| Cadena SER | `ES · NAT · es-ES` | implemented | Keep as Spain default. |
| RNE — Noticias RNE | `ES · NAT · es-ES` | implemented | Keep as Spain default. |
| Onda Cero | `ES · NAT · es-ES` | implemented | Keep as Spain default. |
| ABC — Las Noticias de ABC | `ES · NAT · es-ES` | implemented | Keep as Spain default. |
| COPE — boletines | `ES · NAT · es-ES` | blocked / verify Spotify | Good format; deterministic Spotify identity still required. |

### Latin America / `LAT · es-419`

| Source | Provisional classification | Status | Note |
| --- | --- | --- | --- |
| RFI Español — Noticias de América | `FR · REG · es-419` | reject for current edition architecture | Individual-story cadence risks flooding; useful editorial reference for LAT. |
| N+ Univision 24-7 | `US · MIX · es-419?` | candidate | Strong multi-story daily product; verify exact locale and regional balance. |
| Noticias Univision | `US · MIX · es-419?` | optional candidate | High overlap with N+; do not enable both by default without evidence. |
| Telemundo 52 — Noticiero Digital | `US · LOC/MIX · es-419?` | optional | LA/California-heavy; not a LAT default. |
| VOA — Buenos Días América | `US · REG/MIX · es-419?` | verify active product | Useful if still active and deterministic. |

### World / Spanish from Spain — `INT · es-ES`

No source should be promoted merely because the publisher is European. Spoken locale must be checked from recent editions.

| Source | Status | Note |
| --- | --- | --- |
| ONU en minutos | high-priority candidate | Excellent global bulletin; verify spoken locale, exact RSS product boundary and Spotify identity. |
| DW — Actualidad en análisis | candidate | ~20–25 minute international analysis; verify spoken locale before assigning to ES vs LAT world product. |
| France 24 Spanish products | research | Find a discrete world-news bulletin; do not use LAT-focused or long-form products by mistake. |

### World / Latin-American Spanish — `INT · es-419`

| Source | Status | Note |
| --- | --- | --- |
| CNN — 5 Cosas | implemented source / placement pending | Global scope; verify spoken locale before moving it out of generic `es`. |
| ONU en minutos | high-priority candidate | May be neutral enough for broader use, but locale must be evidenced rather than assumed. |
| N+ Univision 24-7 | candidate | International + US agenda; useful optional world/LAT source if overlap remains acceptable. |
| DW — Actualidad en análisis | candidate | Global content; assign only after spoken-locale verification. |
| SBS Spanish — Noticias SBS Spanish | filtered candidate | Mixed feed; only bulletin-labelled entries should be considered. |

## Other-country and world expansion backlog

These remain independent of the Spain/LAT split.

| Candidate | Proposed classification | Status / reason |
| --- | --- | --- |
| Reuters World News | `GB · INT · en` | high-priority candidate; daily ~10-minute world briefing |
| CBC World Report | `CA · MIX · en` | high-priority candidate; concise Canada/world newscast |
| KBS WORLD Radio News | `KR · MIX · en` | candidate; concise but multi-daily, cadence protection may be needed |
| RNZ News | `NZ · MIX · en` | candidate; concise but several editions/day |
| Czechia in 30 minutes / Radio Prague International | `CZ · NAT · <verified-language>` | candidate; country-news format around the current 30-minute ceiling |
| Tagesschau in 100 Sekunden | `DE · NAT · de` | candidate; very short/high-cadence |
| RTL — Le journal RTL | `FR · NAT · fr` | candidate; concise national bulletin |
| Europe 1 — Le journal | `FR · NAT · fr` | candidate; profile/outliers need measurement |
| TOK FM — Informacje | `PL · NAT · pl` | candidate; characterize anomalous long items |
| Radio ZET — Wiadomości | `PL · NAT · pl` | candidate |
| Sky TG24 news feed | `IT · NAT/MIX · it` | candidate; verify deterministic feed/catalogue |
| Antena 1 — Noticiário | `PT · NAT · pt` | candidate; frequent concise editions |
| VRT NWS update | `BE · NAT · nl` | candidate; verify collection/catalogue contract |
| SRF Nachrichten | `CH · NAT/MIX · de` | candidate; verify deterministic destination path |
| Yle Uutiset / Radiouutiset | `FI · NAT · fi` | candidate; Spotify path unresolved |
| Sveriges Radio — Ekot senaste nytt | `SE · NAT · sv` | candidate; hourly, may need cadence limits |
| DR — Radioavisen | `DK · NAT · da` | candidate; hourly, may need cadence limits |
| BBC Radio 4 — News Summary | `GB · NAT · en` | candidate; verify stable Spotify catalogue path |
| RTÉ news bulletins | `IE · NAT · en` | research |
| NOS short bulletin product | `NL · NAT · nl` | research |
| Radio România Actualități — Știri | `RO · NAT · ro` | candidate; feed/Spotify verification required |

## Long-form / separate-current-affairs backlog

Do not mix these into the concise default products solely because an individual episode fits under 30 minutes:

- FRANCE 24 Español — El Debate
- BBC Global News Podcast
- France 24 — The Debate
- NHK WORLD-JAPAN — In-depth News Features
- EL MUNDO al día

DW `Actualidad en análisis` remains a borderline optional source rather than automatically long-form because recent public samples are generally around 20–25 minutes; production placement should remain configurable.

## Technical intake required before the follow-up PR promotes new sources

For each selected source, verify:

1. official feed/product identity;
2. spoken locale (`es-ES`, `es-419`, or evidenced neutral `es`);
3. editorial scope independently from provider origin;
4. stable RSS/native IDs;
5. deterministic Spotify show ID;
6. title/date/timestamp parser strategy;
7. recent duration distribution;
8. cadence/flood risk;
9. deterministic RSS→Spotify matching;
10. provider-watch and matcher tests.

## Planned follow-up after #130 merges

1. Rebase this branch onto the merged `main`.
2. Update the canonical `SOURCE_SELECTION.md`, `SOURCE_CATALOG.md`, #44 and #53 with this product split.
3. Stop default-sharing generic/Latin-American Spanish sources into the Spain playlist.
4. Promote only technically verified new sources.
5. Add Spain/LAT/world templates only when each has a defensible default source set and matching cover assets.
6. Open a focused PR; do not mix unrelated #130 follow-ups into it.
