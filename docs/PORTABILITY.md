# Portable configuration v1 and staged migration (#542)

Decision baseline: 2026-10-27, main
`5d5256e47b3051e5aa2b5abd3517e080d8276fda`, and the
[owner's revised priority](https://github.com/eXPerience83/news-bulletin-playlist/issues/542#issuecomment-6447871274).

**Implemented:** a pure source JSON encoder/decoder with strict validation,
individual/batch export and lossless definition round trips, plus authenticated
Admin source downloads (#547). Decoding returns data; it does **not** install sources,
change persistence, or activate anything. Authenticated source upload, preview and
explicit apply are implemented in #549. Playlist ZIP export is implemented in #550.
CLI, ZIP import and empty-default image remain future work. Installed-source persistence uses explicit schema v8 (#548),
without automatic migration. ZIP import and clean-image migration remain proposals, not completed functionality. A portable configuration is
not a production backup.

## Dependency and risk matrix

This matrix was established before choosing the interchange representation.

| Existing contract | Verified dependency/risk | Decision and subsequent validation |
| --- | --- | --- |
| `catalog.py`: 13 sources, seven templates | Image-owned definitions remain authoritative for built-ins | Keep them intact. Export effective definitions; later examples must be independent from installed copies. |
| `models.SourceDefinition` | Includes countries and multiple languages, separate origin/scope, timezone, parser, temporal strategy, public mode and filter | Preserve every field, with canonical show identity instead of arbitrary external references. Reuse this immutable domain model for decoded sources. |
| `managed_state.effective_sources` | Applies public-mode overrides, combines/sorts custom sources and rejects show collisions | Caller supplies one effective snapshot, including disabled sources. Never export only the image defaults when local overrides exist. |
| `ManagedSource` / `InstalledSource` / `custom_source_policy.py` | Manual sources retain one language, no countries, `custom_<show>` IDs and restricted processing. Full definitions use the distinct installed representation | Explicit v8 persists complete portable v1 definitions, without widening manual intake. Source import/dedup/apply remains #549. |
| `registry.py`, `spotify/first_catalogue.py` | Universal temporal inference uses the configured temporal strategy; collection filtering is separate. Parser IDs remain compatibility contracts | Version the installed capabilities explicitly. No dynamic code, arbitrary regex, or dispatch on source identity. Keep AUTO and TITLE/PUBLIC/fallback rules intact. |
| `ManagedStateStore` | v1–v5 loads can save migrations; v6 remains v6, drafts opt into v7; save uses temp file, fsync, replace, directory fsync | Pure codec receives data, never a store/path. Future read-only export/preview cannot call mutating load paths unchanged. Apply must revalidate under the configuration lock and save the complete candidate once. |
| `managed_admin.snapshot` / provisioning | Snapshot finalizes a known pending Spotify destination; activation has a separate durable journal | Do not use this snapshot for a side-effect-free portable download/preview. Do not call activation or recovery from import. |
| `SourceAdminService.create` | Performs live inspection before saving | Do not reuse this method for local import. Reuse local validation/collision rules through a separate apply path. |
| `custom_playlist_templates.py` | Existing editable drafts, hidden states, bounded names/descriptions/sources, no duration exceptions | Reuse editor/draft lifecycle. Some managed values exceed draft limits or inherit exceptions: preserve them through a reviewed model extension or block as incompatible, never truncate. |
| `compile_engine_config` | Effective selection drops disabled assignments; policy uses managed values plus template ordering/classifications/exceptions | Export durable `ManagedPlaylist.source_ids`, including dormant assignments, together with the effective policy. Exporting the compiled object alone loses intent. |
| `engine_runtime` synchronization | Cycle gate precedes configuration lock; writes compare the current playlist contract | Preserve lock order and stale-contract checks. Source import must not alter active/pending playlists silently. |
| `CustomCoverAssetStore` / upload handlers | Bounded Pillow decode, metadata stripping, hash-addressed JPEG, no-follow access; JSON and asset writes are separate | Reuse validation/storage. Imported canonical JPEG needs byte-preserving verification, not repeated lossy recompression. Validate everything before publication of state; design recovery for prepared orphan assets. |
| v7 quarantine | Invalid drafts retained as raw JSON, dependent destinations suspended | Never export raw quarantined payloads; block only affected entities. Preserve quarantine and its deletion/reference guards on apply. |
| SQLite/history/reconciliation | `apply_source_refresh` commits rows/history/authority together; transport ambiguity and 429 have established handling | Portable work never touches episode stores, history, caches, cooldowns or reconciler. No extra Spotify calls to serialize/compare. |
| Admin HTTP | Existing authentication, bounded fields, one-use CSRF; cover upload has separate body limit | Separate authenticated download/preview/apply adapters from domain codec. Keep private/no-store responses, safe filenames and existing CSRF on POST. |

Inspection included source and managed Admin renderers/handlers, cover multipart
parser/storage, provisioning and file durability, configuration loading, catalogue
collection, managed-source/state/draft/cover tests and persistence documentation.
This review did not inspect production data; the four custom sources and four real
destinations described by the owner remain operator context, not new local evidence.

Toolchain evidence: development/CI requires Python 3.15; package metadata allows
3.14–3.15 and Ruff/mypy retain 3.14 targets. At this baseline `Dockerfile` actually
pins **3.15.1** (commit `775087e`, #538), despite the older 3.14.8 production wording
in `AGENTS.md`/#494. The deployed interpreter was not inspected. This portability
change does not alter any of those versions or claim production/container validation.

## Source JSON v1: normative contract

`portable_sources.export_sources(effective_sources, built_in_ids=...)` returns UTF-8
bytes. Pass a one-element sequence for an individual export; the same envelope
supports batches. `parse_sources(bytes)` returns immutable `PortableSource` records
containing `SourceDefinition` and informational provenance. `serialize_sources`
re-encodes decoded records without changing provenance. None accepts filesystem
paths, credentials, clients, installation state or activation destinations.

The root has **exactly** `format`, `version`, `sources`. Format is
`news-bulletin-playlist.sources`; version is the integer `1` (not a boolean/float).
All fields below are required; unknown fields are rejected at every object level.

| Source field | Contract |
| --- | --- |
| `original_id` | Original logical reference, 1–128 ASCII letters/digits/underscore/hyphen, beginning with a letter/digit. It is not destination identity or a file path. |
| `spotify_show_id` | Canonical 22-character base62 ID, checked with the shared Spotify identifier validator. No URLs/URIs or normalization guesses. |
| `display_name` | Trimmed, printable Unicode, 1–120 characters. Names are not identities. |
| `origin` | Exact `kind` (`country`/`institution`) and `code`, validated by `SourceOrigin`. `UN` remains institutional. |
| `countries` | 0–10 distinct uppercase two-letter tags; preserve operational metadata independently from origin. |
| `languages` | 1–10 distinct tags: `[a-z]{2,3}(-([A-Z]{2}\|[0-9]{3}))?`, preserving regional variants. This is the existing bounded tag subset, not full BCP 47. |
| `editorial_scope` | Existing `EditorialScope`: LOC/REG/NAT/INT/MIX. |
| `timezone` | Trimmed printable IANA key, at most 128 characters, resolvable by `ZoneInfo`; invalid/absolute/traversal keys fail. |
| `enabled` | Strict JSON boolean; disabled is preserved, not interpreted as removal. |
| `provenance` | `built_in` or `custom` at export. Untrusted informational claim, never permission to overwrite local built-ins or install privileged code. |
| `processing` | Exact `parser`, `temporal_strategy`, `public_release_mode`, `collection_filter`. See capabilities below. |

Each capability is an exact object `{"id": "…", "version": 1, "parameters": {}}`.
`collection_filter` can instead be `null`. Versioned parameters provide a safe
extension point for future declarative rules from #450; **v1 currently accepts only
empty parameters**. Unknown IDs, positive unsupported versions and nonempty
parameters raise `UnsupportedSourceCapability`; malformed types raise
`PortableSourceError`. A later preview can report incompatibility and must prevent
installation/activation. This decoder fails the whole document, not a partial batch.

The explicit portable v1 capability set is:

- Parsers: `ser`, `rne`, `ondacero`, `cnn`, `cope`, `rfi_journal_monde`, `rmf_fakty`,
  `release_date_title`. They must also exist in the installed registry.
- Temporal: `auto`, `full_datetime`, `clock_catalogue_day`,
  `partial_date_catalogue_day`, `explicit_utc`, `disabled`.
- Public release: `instant`, `wall_time`, `disabled`.
- Collection filter: `un_news_es_minutes`, also checked against the registry.

Capability versions identify compatibility semantics, not frozen implementation
bytes. Bug fixes can preserve v1; incompatible semantics require a new capability
version and explicit migration/review. Adding a registry entry alone does not
authorize interchange. No imported value is evaluated or used as an import path.
Parser compatibility, chronology and editorial selection remain separate.

Example processing object (SER):

```json
{
  "parser": {"id": "ser", "version": 1, "parameters": {}},
  "temporal_strategy": {"id": "full_datetime", "version": 1, "parameters": {}},
  "public_release_mode": {"id": "instant", "version": 1, "parameters": {}},
  "collection_filter": null
}
```

Bounds: 256 KiB per document, 1–100 sources, 10 country/language tags per source.
Duplicate JSON keys, repeated logical IDs or show IDs (even identical definitions),
non-finite numbers, malformed/truncated UTF-8/JSON and unsupported versions fail
closed. Encoder and decoder share field validation. Errors do not include supplied
values, keys or underlying parser messages. Export uses explicit fields, never
generic dataclass/state serialization. Additional external-reference kinds are
rejected rather than silently omitted or leaked.

Canonical output has sorted JSON keys, two-space indentation, UTF-8, a terminal
newline and sources sorted by show ID then logical ID. No timestamps or host IDs
make identical exports differ. Classification order is preserved. The current API
does not install anything, so repeated decoding proves serialization stability,
**not yet installation idempotence**.

## Authenticated Admin downloads (#547)

On `/admin/sources`, **Export source** accompanies each source's existing actions,
and **Export all sources** downloads the full effective set, including disabled
sources. These configuration downloads are separate from **Export all audits**,
which performs provider-backed diagnostic work.

- GET `/admin/sources/export.json` downloads `sources-v1.json`.
- GET `/admin/sources/export.json?source_id=ser` downloads `source-ser-v1.json`.
- Existing Admin session and transport checks apply before reading configuration.
  GET does not consume a mutation CSRF token; no JavaScript or Spotify connection
  is needed. Responses use `application/json`, attachment disposition, `no-store`
  and the existing security headers. Filenames contain only a bounded safe ID.
- Query length is limited to 512 characters and at most one `source_id` field.
  Empty, duplicate, malformed or unknown parameters/unsafe IDs return 400; unknown
  configured source IDs return 404. No query selects all sources.
- An empty catalogue/state shows an explanation instead of the batch link; a direct
  batch download returns 409. Codec v1's nonempty requirement remains unchanged.
  Corrupt/unreadable state or codec incompatibility returns a fixed sanitized 500
  response, with no attachment or partial JSON. Missing synchronization returns 503.

`source_admin.export_source_configuration` holds the existing configuration lock
while calling `ManagedStateStore.load_readonly()` exactly once and constructing
immutable effective definitions. It then calls the existing v1 codec. The read-only
loader shares path/symlink checks, duplicate-key handling and the legacy parser with
`load()`, but never saves; older compatible state is upgraded only in memory. Normal
`load()` still persists its existing legacy migrations. No cycle gate is acquired,
so this path never reverses the cycle-gate → configuration-lock order.

The handler supplies the managed configuration store/catalogue and synchronization
directly. Without an OAuth configuration, the runtime has neither source nor managed
Admin services; the download instead reads the same fixed managed-state path under
`data_dir` using the built-in catalogue. Constructing that store does not touch disk.
The download endpoint therefore also works without either Spotify-backed service.

Downloads bypass ordinary service snapshots, provisioning recovery, engine wake,
OAuth, provider clients, SQLite, diagnostics and request metrics. The existing Sources
page's provisioning-aware lifecycle is unchanged; this read-only guarantee concerns
the new download endpoint. Tests compare every synthetic persistent file before/after
and prohibit the operational calls above, including across legacy and failure paths.

## Source import preview and apply (#549)

Source identity is the canonical show ID. Compare full configuration independently
of `original_id` and export provenance. Same show and same config with a different
logical ID means **reuse**, returning an original-to-local mapping. Same logical ID
and identical config is **identical/no-op**. Missing show means **create**. Any
different metadata, enabled state, timezone or processing contract means
**conflict**, with an explicit keep-local/cancel decision; replacement would require
a separately guarded decision. Unknown capabilities mean **unsupported**. A batch
containing duplicate definitions is invalid, not an implicit conflict resolution.

`SourceImportService` separates domain planning/apply from the HTTP adapters and
has no provider, OAuth, episode store or scheduler dependency. Preview inspects the
original bounded document through `inspect_sources()` (see below), then captures the entire
managed configuration, effective definitions, catalogue and pending provision under
`ConfigurationSynchronization`, using `load_readonly()` and the read-only provision
journal loader. It does not call any mutating/service snapshot.

Rows are sorted by canonical show ID. For missing shows, local IDs are always
`custom_<show>` (independent of foreign original IDs), with the first free `_2`,
`_3`, … suffix when that local ID is already occupied by another show. For known
shows, compare every `SourceDefinition` field except logical ID, using the IANA key
for timezone and preserving country/language order. Foreign provenance never grants
permission. Conflicts support only **keep local** or **cancel**; no replacement.
Well-formed unsupported capabilities appear as inert `unsupported` rows. They never
participate in reuse or keep-local decisions, even when their show already exists.
The presence of any unsupported row blocks apply for the entire document.

Apply reloads/replans under the same configuration lock and compares a SHA256
fingerprint of the immutable full state, effective definitions, image catalogue
and pending provision with the preview snapshot. Any configuration/reference or
provision change requires a new preview (409). This uses the existing process-local
configuration synchronization and lock order; no cycle gate or provider lock is
acquired. Every conflict requires a deliberate keep-local choice for that row;
extra or missing choices cannot authorize creation.

New definitions use `InstalledSource.from_definition()` with their allocated local
IDs. Validate the complete sorted candidate with managed-state serialization and
engine compilation; pending source/template references and at least one enabled
pending source must also remain valid. No existing enable/disable setting or
assignment changes, so no disable transition is authorized. Save once, with explicit
v8 adoption acknowledgement when upgrading v6/v7. Existing sources, destinations,
policies, dormant assignments, drafts/quarantine, cover references and journals
remain intact. No scheduler wake, activation or playlist writes follow apply.

Reimport an unchanged document resolves the same original-to-local mapping by show
identity without any save, including in v6/v7. Keep-local-only imports are also true
no-ops. No separate alias file or recovery journal is created. If save reports an
error, read back under the lock: an exact candidate means the complete configuration
is visible (report the durability error and do not save again); exact prior state
means nothing was committed; failed/different readback means uncertain outcome.
Require a fresh preview before retry, never assert rollback after replacement.


### Preview-only compatibility inspection

`inspect_sources(bytes)` is separate from the strict installation decoder
`parse_sources()`. Both share root decoding, byte/count bounds, duplicate JSON key
rejection, exact keys/types, canonical source/show identity and metadata validators.
The inspector validates every capability shape in every row, then rejects duplicate
logical/show IDs across **all** compatible and incompatible rows. Unknown metadata
or malformed documents/capabilities still fail the whole preview (400); an earlier
unsupported capability never masks a later structural error.

Only a **well-formed** unsupported processing contract becomes a diagnostic:

| Valid shape but unavailable contract | Preview reason |
| --- | --- |
| Unknown capability ID | Unknown capability |
| Positive integer capability version other than v1 | Unsupported version |
| Nonempty parameters object | Unsupported parameters |
| Known parser/filter missing from the installed registry | Capability not installed |

Each diagnostic carries a fixed category (Parser, Temporal strategy, Public release
policy or Collection filter) and fixed reason; raw imported capability IDs/parameters
are not interpolated into messages. Multiple incompatible categories per source are
reported in a stable order. Metadata, including a printable name and canonical IDs,
is fully validated even for an unsupported row. Missing capability fields, invalid
IDs, nonpositive/noninteger versions and parameters of the wrong type remain errors,
not unsupported rows. Collection filter `null` remains the supported no-filter case.

Compatible inspection records retain the existing `PortableSource` shape and full
strict validation. An `UnsupportedPortableSource` contains **only** original ID,
name, canonical show ID and diagnostics. It never constructs a `SourceDefinition`,
is not a runtime source and has no local installation mapping. It is not a relaxed
configuration format or permission to execute a capability.

Preview returns HTTP 200 with all rows sorted by show ID, preserving create,
reuse/identical and conflict results for compatible sources alongside unsupported
rows. Names/IDs/diagnostics are HTML-escaped. An unsupported row offers no keep-local
control. A mixed batch shows the entire comparison but prepares no candidate,
installs nothing and hides Apply/v8-adoption controls. Cancellation still works.
The session continues to retain the exact original document and configuration
fingerprint under the same bounded expiry/security rules.

Server enforcement is independent of that presentation: **Apply always calls the
unchanged strict `parse_sources()` contract on the session's original bytes** before
reading configuration or saving. Unsupported documents fail with 422, including
forged confirmations, keep-local decisions, mixed batches and existing-show matches.
No partial installation, no save, no v8 adoption and no state/provider/scheduler
side effects occur. `export_sources()` and `serialize_sources()` remain strict;
inspection diagnostics are not a configuration serialization API.

### Admin source import flow

- `/admin/sources` links **Import sources** to GET `/admin/sources/import`.
- POST `/admin/sources/import/preview` accepts exactly one JSON `document` file and
  a one-use CSRF field in multipart form data. Cap HTTP input at 256 KiB + 8192 bytes
  of framing, then cap the document at 256 KiB / 100 sources with the existing
  shared document validators. A well-formed unsupported capability is a visible
  preview diagnosis (200), while direct apply remains rejected (422). Reject
  missing/duplicate length/type headers, transfer encoding, malformed
  framing, extra/duplicate fields, unsupported transfer/MIME types and truncation.
  Filenames never become paths or get written to disk.
- The preview displays every imported definition, comparisons/differences and
  deterministic original-to-local IDs. It installs nothing. New v8 adopters see
  an explicit compatibility/backup warning; portable JSON is not a production backup.
- Keep one exact upload + fingerprint + random preview reference per authenticated
  session. At most eight previews per credential manager (at most 2 MiB raw JSON),
  each expiring after 10 minutes, with replacement/logout/session expiry cleanup.
  Capacity exhaustion returns 503 with guidance to cancel a preview or retry;
  malformed documents retain their validation errors. Pending documents never enter
  logs, files, diagnostics or client hidden fields.
- POST `/admin/sources/import/apply` uses the existing 4 KiB text form, one-use CSRF,
  the session-bound preview reference, per-conflict decisions, explicit confirmation
  and (when needed) v8 acknowledgement. Both session and document must match. The
  preview is consumed once; replay, expiry, stale or foreign references require a
  new preview. Indexed decisions fit the existing form limit for 100 sources.
- Cancellation is a separate authenticated CSRF form, without a state save.
  Results list created/reused IDs. Existing security/transport checks, escaped
  server-rendered HTML and private `no-store` responses apply. This works without
  Spotify/OAuth-backed Admin services.

The existing Sources overview may still use provisioning-aware snapshots for its
ordinary lifecycle; the dedicated upload/preview/apply routes bypass that behavior.
Tests forbid provider/OAuth/SQLite/scheduler operations and compare synthetic files
before/after previews and no-ops, including pending provisioning and quarantine.

The legacy manual-source model cannot persist all portable v1 definitions. The
#548 implementation chooses option 3 below through explicit v8 installed-source
persistence; #549 builds the explicit importer on it. Options considered:

1. Replace specialized capabilities with generic ones: rejected, lossy.
2. Expand only the parser allowlist: insufficient; it still loses countries/multiple
   languages and does not separate manual intake from reviewed capability support.
3. **Recommended:** extend/version installed-source persistence to represent the
   complete domain definition, using the explicit capability contract; migrate
   existing custom records losslessly. Keep ordinary manual intake policy distinct.
   Implemented below. Unknown capabilities fail closed without rewriting state.
   No new source-specific branching.

## Installed-source persistence (#548)

`InstalledSource.from_definition(SourceDefinition)` validates a complete definition
through the existing portable v1 encoder/decoder and returns immutable domain data.
`source_definition()` reconstructs the exact runtime definition. Neither operation
reads files, calls Spotify, installs a source, resolves conflicts or activates a
playlist. `InstalledSource` extends `ManagedSource` so existing reference/lifecycle
code can handle both without parallel source lists in the runtime model.

The complete ordered `languages` and `countries` tuples are preserved (countries
may be empty). The inherited `language` is a primary-language alias for the existing
Admin editor and must equal `languages[0]`; it is not serialized redundantly.
Metadata editing changes that primary language while preserving additional languages,
countries and processing. A duplicate resulting language is rejected. Parser,
filter, identity and timezone remain immutable in that editor. Existing manual
sources keep their previous representation and validation unchanged.

### Explicit state schema v8

Schema v8 retains all v7 root fields, with these source collections:

- `sources`: unchanged legacy/manual source records, still restricted by
  `custom_source_policy` and deterministic `custom_<show>` identity.
- `installed_sources`: a complete **portable source JSON v1 document**, or `null`
  when there are no full installed records. Its `format`, `version`, capability
  versions and empty v1 parameters use exactly the existing interchange contract.
  It inherits the 100-source and 256 KiB envelope bounds. Unsupported IDs/versions,
  removed registry capabilities and malformed records reject the entire state.

There is no second import schema. The envelope is embedded as JSON, not as an
escaped string. On disk, installed definitions have local `custom` informational
provenance; imported/built-in provenance never grants privileges. `ManagedState.sources`
combines both representations deterministically by logical ID in memory. Duplicate
logical/show IDs across the two collections fail closed. Existing collisions with
the image-owned catalogue are still rejected: this PR does not replace built-ins.
An optional catalogue dependency on parsing/serialization/storage permits isolated
tests of all 13 original definitions without changing production defaults.

Writing v8 requires a deliberate domain operation to construct a candidate with
`schema_version=INSTALLED_SOURCES_SCHEMA_VERSION` and save it. Supplying an
`InstalledSource` in v6/v7 is rejected before writing. No Admin form in #548 opts
into v8. The #549 import confirmation is the explicit Admin operation that may opt
into v8 when adding new sources. Ordinary reads, downloads, metadata saves and draft
creation never upgrade an existing v6/v7 file to v8. Import apply revalidates the entire
candidate under the configuration lock before its single save.

Once explicitly v8, ordinary operations retain v8, including creation of a new
custom playlist draft. No implicit downgrade occurs when its last installed source
is removed. Source assignments (including dormant ones), destinations, duration and
retention settings, overrides, uploaded-cover IDs, drafts and quarantined raw data
retain existing semantics. The provisioning journal is separate and untouched by
source serialization/load/save. SQLite, OAuth and history are outside this change.

| Stored state | Read / ordinary save with this implementation | Rollback |
| --- | --- | --- |
| v1–v5 | Existing `load()` migrations to v6 remain; `load_readonly()` upgrades only in memory | Existing compatibility contract |
| v6 | Remains v6; creating a first draft still explicitly opts into v7 | A compatible v6 reader can read it |
| v7 | Remains v7 | A compatible v7 reader can read it |
| v8 | Read/load/export do not write; ordinary mutations keep v8 | Old images reject v8; restore a matching pre-v8 data snapshot and compatible image |

Before any future explicit v8 adoption, take an installation backup. Portable exports
are not rollback backups. Do not edit a version number to downgrade: that would lose
installed definitions and references. No automatic production migration is included.

The existing temp-file → file fsync → atomic replace → directory fsync save protocol
is unchanged. Failure before replacement preserves old bytes and removes the temp
file. Failure after replacement can report an ambiguous durability outcome; recovery
must read back the complete state, not promise rollback. Tests exercise both points,
legacy byte preservation, pending journal/quarantine/reference preservation, existing
HTTP edits/downloads, and real chronology/filter decisions with synthetic catalogues
for SER, RNE, CNN, RFI, RMF, ONU minutes and the generic custom parser. Network,
OAuth and real Spotify calls are prohibited in the new domain tests.

## Portable playlist ZIP v1 (#550)

Admin managed-playlist and local-draft cards offer **Prepare portable export identity**
until that object has an identity, then **Export portable ZIP**. Preparation opens
an authenticated confirmation page; it is not a download side effect. Downloads
are authenticated `GET /admin/playlists/export.zip?kind=managed|draft&id=<local-id>`.
Preparation uses a separate `GET`/`POST /admin/playlists/portable/prepare`.

### Persisted identity and schema v9

Each managed playlist and local draft has its own nullable `portable_id`, a canonical
lowercase UUIDv4. A draft and its managed destination are separately editable objects
and therefore have distinct identities. Names, template IDs and Spotify destination
IDs do not determine identity. UUIDs are generated with OS randomness and checked
against other accepted local objects. Normal edits retain identity. Future #551 must
preserve an imported UUID and compare content; matching names do not authorize merging.
Quarantined drafts remain raw, are not exported, and cannot bypass UUID uniqueness
when repaired. Identity uniqueness is enforced across all accepted managed/draft records.

Schema **v9** explicitly extends v8: every managed playlist and valid local draft
record requires a `portable_id` key (`null` means not prepared). All v8 root fields,
installed sources, overrides, destinations, quarantines and pending-provision behavior
are preserved. No sidecar mapping exists. Readers accept v6/v7/v8 unchanged; ordinary
reads, startup, source downloads, playlist downloads and ordinary legacy saves never
adopt v9. Legacy export without identity fails with an explanation.

Only an explicitly confirmed identity preparation may initially adopt v9. Its form
requires one-use session CSRF, current configuration/catalog/target fingerprint,
confirmation and (below v9) a separate compatibility acknowledgement. Under the
configuration lock it re-reads, validates the complete candidate and saves once using
the existing atomic protocol. Save errors trigger readback: confirmed new state is
reported as committed, confirmed old state as unchanged, otherwise outcome is uncertain;
there is no blind retry. Pending provisioning is never finalized by preparation/export.
Once v9 is adopted, creation of a new local draft or finalization of an explicitly
requested managed destination gives that new object a UUID; existing unprepared
objects still need preparation. Retrying known provisioning preserves the saved UUID.
Source imports and all ordinary mutations retain v9 without downgrade.

| On-disk schema | Read/download behavior | Explicit preparation | Rollback |
| --- | --- | --- | --- |
| v6/v7/v8 | Read-only, no identity generation; unprepared ZIP blocked | Confirmed operation writes v9 | Older images retain their original schema support until adoption |
| v9 | Identity preserved, download never writes | Fill only the selected missing identity; prepared objects are no-ops | Pre-v9 images reject v9; restore a matching complete backup and compatible image |

Take an operator-managed complete backup before adoption. Do not edit the schema
number to roll back. A portable JSON/ZIP is **not** a production backup. This feature
does not copy or migrate any real production data in a development session.

### Exact archive contract

Exactly four entries, in this order (no directories, extra fields or archive comment):

1. `manifest.json`: canonical UTF-8 JSON, sorted keys, two-space indentation, final newline.
2. `manifest.sha256`: lowercase SHA256 of the exact manifest bytes and a final newline.
3. `sources.json`: the existing strict portable sources JSON v1, without modification.
4. `covers/<sha256>.jpg`: the exact selected local JPEG bytes, addressed by SHA256.

ZIP uses stored entries (no compression), fixed 1980-01-01 timestamps, Unix regular
file mode 0600, no ZIP64 and deterministic metadata. The whole archive is built and
validated in bounded memory before a successful attachment response. Limits: 1 MiB
archive, 256 KiB manifest, existing 256 KiB/100-source codec limits, 192 KiB JPEG,
4096-pixel dimensions and 16 million decoded pixels. There are exactly four files.
`custom_default` is a valid existing 192-pixel JPEG and remains byte-preserved.
Integrity digests detect corruption; they do not authenticate authorship.

The manifest has exactly these top-level keys:

- `format`: `news-bulletin-playlist.playlist`; `version`: integer `1`.
- `playlist`: the fields listed below.
- `files`: descriptors `{size, sha256}` for `sources.json` and the cover path.

`playlist` contains:

- `portable_id`, informational `original_id`, `kind` (`managed` or `draft`).
- Exact `display_name`, `description` and observational `configuration` with
  `{enabled: boolean|null, hidden: boolean|null}`. Managed objects have `hidden: null`;
  drafts have `enabled: null`. These flags are not activation instructions.
- Ordered `countries`, `languages`, durable ordered `source_ids`, and `ordering`
  (`edition_at_desc`). Source definitions themselves follow source-v1 canonical order.
- Exact integer `retention_hours`, `max_episodes`, and `duration_policy` containing
  `min_seconds`, `max_seconds`, `exceptions`.
- Each duration exception has `id`, `source_id`, `edition_local_time` as
  `HH:MM:SS.ffffff`, `edition_fold` (0/1), and exact `max_seconds`.
- `cover` contains `original_id`, `origin` (`bundled`/`uploaded`), `path`, `media_type`
  (`image/jpeg`), `size`, `sha256`, and `attribution` with `credit`, `license`,
  `provenance`, `reference`. Unknown credit/license/reference is JSON null.

Managed objects combine their exact saved name/description/cover/assignment/policy
with template geography, languages, ordering and applicable duration exceptions.
An exception applies when its source remains durably assigned (including disabled
sources) and its ceiling exceeds the managed base ceiling, as in runtime policy.
Drafts retain all local fields, including hidden status. Current local drafts have
no exception model, so their exception list is empty. Exports do not use compiled
membership, which would drop disabled assignments.

Logical IDs are bounded source-v1-style identifiers (128 characters). Text limits
are 4096 Unicode characters for names and 65536 for descriptions; policy integers
are bounded by signed 64-bit maximum; geography/language lists and exceptions by 100.
These export bounds deliberately exceed editor limits. Values that cannot be represented
are rejected with a safe explanation, never truncated. Empty assignment exports are
rejected because source v1 requires at least one definition. #551 must extend draft
representation explicitly or reject legacy policies it cannot represent; it must not
round seconds, truncate values or drop exceptions to fit the current editor.

All assigned definitions come from `effective_sources()`, including overrides,
installed specialized capabilities, manual definitions and disabled sources. The
existing strict source codec validates the closure with one definition per canonical
Spotify show ID; any missing reference/capability or ambiguous duplicate fails closed.
Original source IDs remain in the manifest and source document for future mapping.

Bundled covers are allowlisted and opened locally with no-follow, nonblocking,
regular-file checks and bounded reads; uploads use `CustomCoverAssetStore` integrity
checks. JPEG decoding is bounded and never recompresses or downloads artwork.
Missing/corrupt/unsafe assets block export, without fallback to a different cover.
Available curated SVG-master provenance is recorded with a documentation reference;
uploaded/default rights are not invented. Software MIT is not an artwork license.

Downloads and confirmation GETs use `load_readonly()` under
`ConfigurationSynchronization`, without provider-backed Admin snapshots, OAuth,
Spotify, SQLite, scheduler wake or provisioning finalization. Responses are private,
no-store, have safe UUID-based filenames and existing security headers. They contain
no destinations, credentials, operational history, journals, diagnostics or host paths.

### Deferred ZIP import (#551)

ZIP parsing, hostile-path/duplicate/bomb validation, UUID/content conflict handling,
source mapping, editable recipient draft creation and cross-file asset installation
are not implemented here. The importer must validate the entire package before any
publication and retain identities and exact configuration. Content-addressed asset
preparation must use existing atomic storage and define crash recovery; hashes alone
do not establish trust or grant permission to overwrite local objects.

An imported playlist is always an editable local draft. Explicit activation keeps
using existing #518/#528 provisioning. Do not confuse successful JSON round trips
with complete, crash-tested cross-file installation.

## Clean image and compatibility strategy (deferred)

Separate engine capabilities, available source examples, installed sources,
available playlist examples, user drafts and managed Spotify destinations.
Installed definitions must not change when an image updates/removes an example.
Do not remove built-ins before import/export and isolated acceptance work.

| Installation | Proposed behavior |
| --- | --- |
| Truly fresh, independent empty data root | No implicitly installed sources or destinations; examples shown for explicit selection/import, without provider traffic. |
| Existing v6/v7 | Retain current effective behavior until explicit reviewed migration; preserve all legacy source/template IDs, overrides, destinations, assignments, settings, covers, auth, history and pending/quarantined state. |
| Legacy migration selected | Materialize effective built-in definitions and needed template policy into installation ownership with stable references, versioned atomic state transition and verified backup. No destination recreation or automatic activation. |
| Rollback | Restore matching state snapshot and compatible image as documented for v7; image-only rollback cannot be promised after a future schema upgrade. Never reset production data. |

Absence of `managed-state.json` alone is not sufficient evidence of an unused data
directory; auth/history/provisioning may remain. Test explicit classification and
interrupted migration. Do not invent a startup reset or irreversible automatic
migration. All clean-install tests use synthetic temporary roots/independent volumes.

## Reviewable work sequence

| Issue / focused PR | Scope and dependency |
| --- | --- |
| [#546](https://github.com/eXPerience83/news-bulletin-playlist/issues/546) | This source codec, contract, risk matrix and tests. |
| [#547](https://github.com/eXPerience83/news-bulletin-playlist/issues/547) | Read-only snapshots and authenticated individual/batch Admin downloads. |
| [#548](https://github.com/eXPerience83/news-bulletin-playlist/issues/548) | Lossless installed-source capability/persistence support; prerequisite to apply. |
| [#549](https://github.com/eXPerience83/news-bulletin-playlist/issues/549) | Source preview/conflicts/mapping and atomic apply/UI, after #548. |
| [#550](https://github.com/eXPerience83/news-bulletin-playlist/issues/550) | Effective self-contained playlist ZIP export and portable identity. |
| [#551](https://github.com/eXPerience83/news-bulletin-playlist/issues/551) | Secure ZIP preview/apply as drafts, asset recovery and idempotence. |
| [#552](https://github.com/eXPerience83/news-bulletin-playlist/issues/552) | Optional defaults and explicit lossless legacy migration, after portable round trips. |
| [#553](https://github.com/eXPerience83/news-bulletin-playlist/issues/553) | Isolated empty-install end-to-end acceptance and migration/rollback evidence. |

Existing #259/#516–518 and #527/#528 implementations are reused; their outstanding
operator acceptance is not replaced by new issues. #541 is resolved. #450 editorial
rules, source expansion, Python/Docker promotion and production deployment remain
separate. No merge, Edge publication, TrueNAS change or real Spotify action is
authorized by this design.
