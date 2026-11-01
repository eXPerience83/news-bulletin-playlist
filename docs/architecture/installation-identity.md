# Installation identity and optional presets — staged design record (#552)

Status: **read-only preflight plus isolated Linux opt-in marker writer**. Neither
is connected to startup. No runtime source compilation, managed-state
serialization, Admin, Spotify or persistent production /data behavior changes.
Do not enable new-install presets or deploy a migration based on this stage.

## Existing contract and the hazard

The image ships 13 sources and seven managed-playlist templates. The effective
source compiler always prepends image-owned sources; installed sources reject
built-in source-ID and Spotify-show-ID collisions. Reading a missing
managed-state.json returns an empty default state. However, a real legacy root
can already contain SQLite, OAuth, pending provisioning, covers or other journals.
engine_runtime.serve() currently initializes SQLite before loading configuration.

**Missing managed-state is not evidence of first installation.** Never delete
built-ins, relabel them ordinary customs, or change live defaults in place.

## Proposed durable contract (only the isolated writer can create it; not used by runtime)

An installation-owned file named installation-identity.json will contain exactly:

~~~json
{
  "schema_version": 1,
  "installation_id": "<canonical random UUIDv4>",
  "mode": "legacy_compatible"
}
~~~

The second allowed mode is "opt_in", reserved for genuinely new roots. Mode is
immutable on ordinary restart; turning a legacy installation into opt-in will
require a separate explicit migration with validated materialization and backup.
An identity marker does not grant permission to contact Spotify or activate
destinations.

The read-only inspect_installation_root() returns:

| Pre-bootstrap evidence | Disposition | Meaning |
| --- | --- | --- |
| Missing or empty real directory, no marker | empty_unclaimed | Candidate only; writer requires an existing real empty directory |
| Any entries without marker, even hidden/unknown or broken symlink | legacy_compatible | Preserve legacy behavior and durable state |
| Valid legacy_compatible marker | claimed_legacy | Existing installation retains legacy mode |
| Valid opt_in marker | claimed_opt_in | Future runtime may use optional presets after schema gates |
| Corrupt marker, unsupported schema, symlink marker or unsafe root | Error | Fail closed; never silently switch mode |

A preflight observation is **not a lock**. The isolated stage-2 writer handles
cooperating bootstrap concurrency and exclusive marker publication, but **it is
not yet called by startup**: a later PR must sequence its use before SQLite,
OAuth, health, journal or Admin writes. No existing directory containing durable
evidence may be assigned opt-in merely because its managed-state.json is missing.

### Stage 2: explicit guarded claiming primitive (not enabled in startup)

The isolated `claim_pristine_installation_root(data_dir)` primitive is designed
for an **existing, empty, real directory on Linux**; it does not create an
absent /data mount. It opens the directory without following root symlinks,
takes an advisory exclusive `flock` on its directory descriptor (avoiding a
polluting bootstrap lockfile), revalidates the root and entries, and returns
existing identity evidence or legacy compatibility **without changing it**.

For a truly empty root only, it writes a strict UUIDv4 opt-in marker to an
exclusive mode-0600 staging file under the locked directory, fsyncs the
staged bytes, checks for any newly appeared foreign entry, and publishes via
an exclusive hard link (never replacing an existing marker). It fsyncs the
directory after publication and after unlinking the staging name. If a
pre-publication failure occurs, cleanup is best-effort; abandoned files are
treated as legacy evidence, not proof of freshness. If a failure happens
after linking, the outcome is explicitly uncertain: the marker may already
be present and a restart must inspect and honor it, never blindly retry
or overwrite a UUID. Parallel cooperating claimers receive the same identity.

Advisory locking **does not coordinate independent actors that ignore the
lock**. As with all filesystem bootstraps, ensure the data root has one
writer owner during first start. The final startup integration must call
this protocol before SQLite/OAuth/health/init can create evidence and must
fail closed on unsupported storage/locking. No caller in the app uses
the primitive yet, and it must not be manually invoked on production.

The marker is authoritative only if created by this guarded protocol.
This helper is neither a broad security boundary nor a migration executor.
Do not retrofit opt-in markers onto old installations manually.

## Stage 3: in-memory legacy materialization preview

The pure `preview_legacy_materialization(catalog, state)` helper explicitly
models the next stage without mutating a data directory. Given a parsed legacy
managed-state v6–v10 and the currently running image's catalog, it snapshots
the **effective** 13 image presets with their per-installation source overrides,
plus all installation-owned sources, preserving complete parser, temporal,
filter, language, country, enabled and Spotify show identity metadata. Each
source is round-tripped through the existing installed-source v1 capability
contract and compared byte-for-byte at the semantic object level. The preview
also freezes the seven image templates and surviving local drafts with separate
provenance, together with the original unmodified managed state and compiled
runtime view.

No output is persisted: the preview deliberately keeps existing destination
IDs, journal/OAuth/SQLite paths, cover asset bytes, disabled or dormant selections,
quarantine, and previously accepted writes outside its reach. A frozen in-memory
plan is **not** a migration, a backup, a usable opt-in catalog, or a new v11
format. It is a gate for designing explicit persistent provenance and rollback
without allowing ordinary installed sources to collide with existing built-in
IDs. Existing open tasks still include actual v11 storage, schema migration,
old-image rollback and any startup/UI opt-in behavior.

## Stage 4: explicit frozen legacy sidecar (no runtime integration)

The standalone `legacy_catalog_snapshot.py` module serializes the **effective**
v6–v10 source definitions, distinct origin flags (image preset vs local install)
and full playlist-template metadata, including source memberships, time bounds,
duration exceptions and fold bits, into a deterministic UTF-8 document:
`frozen-legacy-catalog.json`, schema v1. A SHA-256 of the exact original
`managed-state.json` bytes binds the sidecar to the source document; the
original parsed managed state is revalidated against the preview before
serialization. The strict decoder checks schema, capability support,
provenance, source identity, references and duplicate template IDs. It does
not grant runtime authority or set new-install opt-in mode.

The **explicit**, separately invoked writer obtains a Linux advisory directory
lock, checks the original state, creates a restricted mode-0600 staging file,
fsyncs the bytes, rechecks state, then publishes through an exclusive hard
link (no replacement) and directory fsync. Repeated writes are allowed only
if byte-identical; corrupted or divergent existing sidecars fail closed.
An interrupted/uncertain publish must be inspected and must never trigger
a blind overwrite or a Spotify operation. The service has **no caller** in
runtime/startup, Admin or automated migration paths.

The sidecar contains **definitions only**, not Spotify destination IDs, OAuth,
SQLite history, pending journals or cover JPEG bytes. It is intentionally
**not a backup**, not a live migration to v11, and not sufficient by itself
to allow retiring the legacy built-ins. A full rollback still requires a
consistent snapshot of the existing /data volume and image digest. No image
publication/TrueNAS operation is part of this stage.

## Stage 5: read-only v11 ownership projection

The pure `project_v11_legacy` function requires both a frozen catalog sidecar
and the matching original managed-state bytes. It reconstructs a historical
catalog from **frozen** image presets, never from the current image. It checks
the exact SHA-256, effective sources, examples and references, and separates
installed sources, example provenance, local drafts, quarantined drafts and
managed Spotify destinations into an immutable candidate.

**New blocking discovery:** frozen sidecar v1 canonicalizes sources by
Spotify show ID and therefore discards the original built-in source iteration
order. The projection verifies identical source objects by logical ID and
playlist ordering, but exposes `source_order_verified=False`. It cannot
authorize an exact runtime cutover. A separately versioned sidecar update
must preserve original effective source order and acceptance must compare
ordered runtime configurations before v11 migration is permitted.

This is not a schema-v11 serializer or runtime change. It does not write files,
activate playlists or change legacy startup. Migration transactions, rollback
and opt-in new installations remain separately gated.

## Stage 6: source-order-preserving frozen sidecar v2

Sidecar **v1** (`frozen-legacy-catalog.json`) remains readable and its
original bytes immutable. Portable source serialization sorted definitions
by Spotify show ID, so it **cannot prove** legacy source iteration order.
Reading v1 continues to report `source_order_verified=False`.

Sidecar **v2** (`frozen-legacy-catalog.v2.json`) adds a strict
`source_order` array of logical source IDs from the *original effective
runtime*, before any source export sorting. The v2 decoder checks exact
membership, uniqueness and type, and the pure v11 projection reconstructs
the **historical built-in catalog order** independently of current image
presets. It fails closed unless the reconstructed runtime order matches
the v2 field. Tests compare full ordered `EngineConfig` objects, including
all four destinations, across synthetic v6–v10 states.

The explicit v2 writer publishes a separate, exclusively created file,
never upgrades or replaces a v1 sidecar, and checks any existing v1 evidence
for exact original-state digest, source definitions, provenance and templates.
Corrupt or conflicting v1/v2 artifacts are blockers, not invitations to
repair or overwrite. Versioning and verified provenance still **do not**
create a v11 on-disk managed-state codec, startup integration, rollback
transaction or permission to deploy. A matching full /data snapshot is
still required for later recovery.

## Stage 7: strictly validated, self-contained v11 candidate envelope

`v11_candidate.py` defines a **pure schema-v11 migration-candidate codec**,
not a replacement for the existing `managed-state.json` v6–v10 reader.
It requires an exact, ordered frozen-v2 catalog and the matching raw legacy
state bytes; the pure projection must reproduce the historical ordered
`EngineConfig`. The candidate's distinct sections contain installed
sources (with their source provenance), ordered source IDs, playlist examples,
installation-owned drafts, quarantined drafts, source overrides and existing
managed destinations. It also encloses the exact old managed-state bytes and
frozen-v2 bytes as bounded base64, with independent SHA-256 checks.

The strict reader reconstructs all sections from those original bytes, rejects
duplicate/unknown fields, malformed base64, version ambiguity, corrupt,
noncanonical or partially edited payloads, and returns the exact old bytes for
a future byte-for-byte rollback. The envelope is **not an authentication
signature**, full /data backup, or source of authority for live Spotify calls.
It contains private configuration and destination IDs and must be treated
as sensitive.

**Not wired into startup or written automatically**: a later PR must add a
separately tested, locked, crash-safe disk publication transaction, its rollback
procedure, and a genuinely versioned v11 `ManagedStateStore` reader with
explicit rollback compatibility before any production migration. Until that
gate passes, old-image rollback relies on the corresponding matching /data
snapshot, including SQLite, OAuth, covers and journals.

## Stage 8: isolated, no-overwrite candidate publication and recovery

The **explicit** `v11_candidate_storage.py` component stages the already
validated v11 candidate into `managed-state-v11.candidate.json` (0600)
in an existing real Linux data directory. It requires a matching on-disk
legacy `managed-state.json` and `frozen-legacy-catalog.v2.json`.
It obtains an advisory directory `flock`, rejects symlinks/nonregular
evidence, builds/validates the candidate, writes to an exclusive staging
file, fsyncs bytes, **rechecks both original input files**, and publishes
the candidate using an exclusive hard link (never `os.replace`). It
fsyncs the directory after publication and again after removing the
staging name. An existing candidate may be reused only if *byte-identical*.

A pre-link failure does not replace/create a managed-state or candidate
file; best-effort cleanup may leave a staging artifact. If an error occurs
**after the hard link succeeds**, the marker can already exist but its
durability is uncertain; the code fails closed and **retains evidence**,
never fabricates success. On a subsequent explicit inspection, a valid
matching marker can be examined without changing any original file.
Other independent writers that ignore directory `flock` remain outside
its transactional guarantee: this helper is not ready to run concurrently
with a live application's ordinary state writes.

`read_v11_candidate` is read-only and returns the verified candidate,
including original managed-state bytes. In normal mode it also checks that
the live legacy managed-state still matches. In explicit recovery mode,
it can read the preserved old bytes if the live state has subsequently
changed, **without modifying the changed state**; the on-disk frozen-v2
sidecar must still match the candidate exactly. No recovery routine
automatically writes files, touches Spotify or operates on production.

This is **not** a v11 `ManagedStateStore`, a complete /data backup, a
rollback transaction, or a migration cutover. It does not capture OAuth,
journals, SQLite history or cover JPEGs; those need independent consistent
dataset snapshots and rollback tests. No startup path invokes this module.

## Stage 9: independent, read-only v11 owned-state reader

`v11_state_reader.py` introduces an **isolated read-only interface** for
the persisted, separately published `managed-state-v11.candidate.json`.
It accepts only the fully verified schema-11 candidate and re-compiles the
runtime from its **owned sources and provenance**, frozen image-owned
examples, local drafts, managed destinations, quarantine and source
overrides. It does not consult `BUILTIN_CATALOG` from the running image.
It fails closed if the independently reconstructed runtime differs from
the exact historically validated projection, including source order.

`load_v11_readonly_state` normally checks the unchanged legacy
`managed-state.json` and exact frozen-v2 evidence through the candidate
storage reader. A deliberately requested recovery inspection permits
reading the verified original bytes after the legacy file changes, but
**never writes those bytes back or activates any Spotify destination**.
The returned state always reports `is_live_migration_authorized=False`.

This is a v11 candidate **read path**, not a replacement for
`ManagedStateStore`, not a general-purpose v11 persisted state editor,
and not the cutover itself. The current v6–v10 path remains unchanged.
Before production opt-in, separate PRs must provide an independently
versioned v11 state transaction, consistent whole-/data rollback, old-image
compatibility and startup/Admin integration. No normal startup calls
this reader.

## Stage 10: self-contained schema-v11 owned-state codec (not active)

The pure `v11_operational_codec.py` is the first standalone **operational
read-model document** that can reconstruct historical sources, templates,
managed playlists and policies without opening the original
`managed-state.json`, looking up any preset from the **current image**, or
using SQLite/OAuth/Spotify. It is **not yet a writable runtime store**.
A strict schema-11 document contains the fully validated ordered frozen-v2
catalog plus a canonical managed overlay and a migration-origin digest.
Ownership and source order are self-contained rather than inferred from
whatever presets happen to ship with a newer container image.

`build_v11_operational_from_candidate` accepts **only** a verified
historical candidate, serializes the old state into its canonical supported
overlay version, and requires the resulting standalone runtime to match the
full historical `EngineConfig` exactly. `parse_v11_operational` decodes
without original legacy files; it rejects duplicate/unknown fields, malformed
schemas, noncanonical JSON, missing/duplicate/unordered sources, unsupported
capabilities or references, and any mismatch between frozen effective source
definitions and reconstructed runtime. The embedded origin SHA-256 detects
cross-file confusion but **is not a signature or backup**. Valid documents
can include private playlist destinations; do not publish them publicly.

**Cutover remains explicitly forbidden.** The new reader is neither wired
to `ManagedStateStore` nor responsible for saving local edits. Existing
installations still need a reviewed one-shot transaction, consistent /data
snapshot (SQLite/OAuth/covers/journals), crash-safe resume/rollback protocol,
and old-image compatibility acceptance before schema v11 can be activated.
The codec never writes files or contacts Spotify; a valid result always
reports `is_live_migration_authorized=False`.

## Stage 11: offline whole-dataset inventory and rollback acceptance (read-only)

The new independent `offline_dataset_inventory.py` is a **read-only
integrity inventory** for a separately copied, stopped synthetic /data root.
It hashes all regular files (including SQLite and WAL/SHM, private OAuth,
pending journals, custom cover files, old managed state, frozen catalog and
candidate), records paths/sizes/modes, and verifies a second scan matches
the first. It fails closed on missing roots, symlinks, FIFOs/sockets/devices,
unexpected mounted subdirectories, inode/size/mtime races and changed files.
It does not write a manifest, copy files, provide backup credentials or
access any provider. The inventory itself is **not** a full backup, a
transaction log, a proof of database consistency or a cutover token;
`authorizes_migration=False` always.

Synthetic acceptance copies a complete test dataset to independent roots,
compares inventories, corrupts a disposable source tree, then verifies the
original backup and a new restored copy still match their original contents.
This verifies an **offline fixture**, not the ability to restore a live NAS.
External quiescence, coherent SQLite snapshot/stop, permissions/ownership and
a rehearsed rollback of the *actual* application dataset require explicit
operator control and separate acceptance before migration. This tool must
not be run against a running application's production /data.

## Stage 12: offline-only operational v11 shadow staging (no cutover)

`v11_shadow_staging.py` is an isolated staging **research primitive**,
not a migration executor. It requires a caller to explicitly assert the
application is offline and to supply a **separate fully inventoried backup
directory** whose contents match the source before staging. It rejects
the source as its own backup, backup paths nested inside source (or vice
versa), source/backup hardlinks, unsafe file types, symlinks, changed
source/backup file bytes and mismatched destination/journal/cover evidence.

Only after verifying original `managed-state.json`, ordered frozen-v2
snapshot and candidate does it create
`managed-state-v11.shadow.json` with a Linux directory lock,
0600 exclusive staging, file fsync, second complete inventory scan and
exclusive hardlink publication. It never replaces or renames the
legacy state, deletes old evidence, selects the new state in runtime,
or alters any Spotify destination. Existing shadows must be bit-identical;
post-link durability uncertainty preserves the marker and temp evidence for
manual inspection, with no blind repair.

The read-only inspector compares the shadow against the independently
reconstructed operational v11 document and the untouched backup
inventory. **Both results always deny cutover.** A boolean caller assertion,
two matching inventories and a dry-run of synthetic restoration **cannot**
prove true process quiescence, WAL/SQLite checkpoint consistency, ownership
and ACL preservation, crash safety of an actual data-volume switch, or
old-image rollback in production. The helper is **not authorized to
run against live TrueNAS /data**. That requires a separately developed
whole-dataset snapshot protocol and explicit operator acceptance.

## Pending migration decisions and guardrails

1. Freeze effective built-in source definitions, parser/filter capabilities and
   stable source/show IDs as installation-owned snapshots with distinct provenance.
   Do not reuse the ordinary custom-source namespace. Materialize the seven
   templates, preserving overrides, dormant memberships and policies.
2. Preserve all four public playlist records and Spotify destinations, names,
   covers, source membership, duration/retention/caps/ordering, portable IDs,
   enabled flags, quarantine, SQLite history, OAuth, uploaded cover bytes and
   all provisioning and partial-reconciliation journals. No provider writes.
3. Introduce a separately reviewed state schema (likely v11), strict validation
   and fail-closed older-image handling. Rollback must restore a complete
   consistent /data snapshot, not only JSON or a portable ZIP. The existing
   portable export is **not a production backup**.
4. Test v6–v10 historical roots, missing-state-but-existing-data, migration
   interruption at every write boundary, atomic persistence, restart,
   idempotency, and older-image rollback before changing runtime behavior.
5. Only afterward implement optional preset cloning/import and separate the
   Admin presentation of available examples, installed sources, local drafts and
   managed destinations. New image presets cannot silently mutate frozen
   installed copies. Showing/importing examples never triggers Spotify.

## Review gates

Stage 1 covers read-only empty/legacy/symlink/identity recognition. Stage 2 adds
the separately invoked marker writer and isolated failure/concurrency tests.
Stage 3 adds a pure v6–v10 materialization preview and synthetic fixtures.
Stage 4 adds an explicit standalone frozen-catalog sidecar store, never
invoked by startup. No stage yet changes managed-state or runtime catalog
compilation behavior.

Follow-up PRs under #552: versioned persistent legacy provenance and
rollback fixtures; guarded pre-storage startup integration only after those
migration gates; fresh opt-in configuration and explicit preset import; and
runtime/Admin separation. The guarded marker-writing primitive alone does not change defaults.
#553 is independent empty-install round-trip acceptance; #565 remains the beta
release gate.

## Stage 13: offline backup filesystem-metadata fidelity (not cutover)

The whole-/data inventory now includes file and directory UID/GID, POSIX
permissions, and hashes of extended-attribute values (never their private
contents), plus file hardlink counts. It detects ctime/owner/mode changes
during file hashing. The offline v11 shadow stage therefore refuses a
backup even when all JSON, database, OAuth and cover bytes match if metadata
differs. Isolated tests compare copied/restored roots and deliberate file,
directory and extended-attribute corruption.

**Still not authorization for migration:** ZFS/NFSv4 ACLs or other platform
metadata may not be represented by POSIX xattrs, so a matching comparison
is not a complete filesystem snapshot. It cannot prove service quiescence,
SQLite WAL coherency, a restorable NAS backup, or old-image compatibility.
`authorizes_migration=False`, `permits_cutover=False`; no startup/TrueNAS,
GHCR or Spotify change is introduced.


## Stage 14: read-only SQLite/WAL recovery acceptance (still no cutover)

The independent `offline_sqlite_backup.py` helper requires an explicit
offline-operator assertion and a matching complete /data backup under the Stage
13 file/metadata gate. It securely copies only the known runtime database
`news-bulletin-playlist-v1.sqlite3` and its optional `-wal` into a private
temporary directory, checking copied SHA-256 evidence. SQLite's
`PRAGMA integrity_check` and schema inspection operate **on the scratch
copy only**: -shm regeneration or WAL recovery never writes original /data
or backup files. A missing/corrupt database, unresolved rollback journal,
orphan -shm, mismatching backup or changed source evidence fails closed.
Tests include a real uncheckpointed committed WAL, checkpointed SQLite,
independent restored dataset, missing/corrupted evidence and refusal to run
without the offline assertion. No startup/ManagedStateStore/TrueNAS caller;
`permits_cutover=False` is unconditional.

**Boundaries:** reading the copied SQLite database is *not* proof that the app
was actually stopped, a complete WAL tail was captured, that a ZFS snapshot
was atomic, or that native NFSv4 ACLs have been restored. The old pre-0.1
database is merely inventoried and is not opened by this helper. Before
activating v11, an operator must independently establish application
quiescence, snapshot the whole data volume including ACLs and ownership,
retain the old image digest, and rehearse a disconnected old-image rollback
against the fully restored data tree. No provider writes or release involved.

## Stage 15: complete legacy-read rollback rehearsal on synthetic roots (not image validation)

The independently invoked `offline_legacy_rollback.py` requires three
distinct root directories: the unchanged legacy + v11 shadow source,
an original independent backup and a disposable restored tree. It refuses
nested, linked or discrepant roots. They may share a physical storage device:
this check is not evidence of off-device backup durability. It checks the backup against the shadow
evidence and the restored tree against the *complete* backup inventory,
including managed state, OAuth, journals, covers and file metadata. The
standalone Stage 14 SQLite/WAL integrity check runs against yet another
temporary copy. A read-only legacy `ManagedStateStore` is then instantiated
using the frozen historical catalog, not the current image defaults; its parsed
v6–v10 state, complete ordered runtime and exact managed destination IDs must
match the verified v11 shadow. Rechecks ensure none of the three input trees
changed. Synthetic tests cover all five schemas, all four destination records,
corruption, missing shadow, path aliasing and zero network activity.

This is **not** an old-container execution test: `ManagedStateStore` here is
the project's current legacy read implementation. It does not prove that a
deployed older image will accept a future schema, or that NAS ACL/ZFS
properties survive physical restoration. The helper cannot create or restore
a production backup, and always returns both
`proves_old_image_compatibility=False` and `permits_cutover=False`.
Remaining blockers: operator-controlled quiescence, coherent ZFS snapshot with
native ACLs and all data, isolated boot of an exact pinned previous image
without provider access, and then a reviewed journaled migration transaction.


## Forward-only v11 implementation — first writable owner-store slice (#552)

Owner decision on 31 October 2026 supersedes old-image downgrade/rollback gates:
**breaking upgrade, no supported downgrade**. The first functional increment is
`V11OperationalStore`, using `managed-state-v11.json` as a separately invoked
authoritative file. `initialize_from_legacy` freezes the exact installed
legacy catalog and builds schema v11 directly from managed-state v6–v10 in
memory, then publishes the new file with an exclusive link, fsync and no
overwrite. The original `managed-state.json` is retained unchanged as inert
migration evidence. Source order, disabled assignments, four destination IDs,
portability metadata and local drafts are checked during serialization.

The first schema-v11 managed-overlay writer supports validated source/template
and playlist changes through immutable historical image definitions, with
an exact-byte optimistic concurrency check, private temporary file and atomic
replacement. A malformed or missing v11 state is an error; no silent fallback
to legacy. OAuth, SQLite, cover files, and reconciliation journals are untouched.

**Not yet wired to runtime or Admin.** The old runtime keeps its original file
until the separate integration PR replaces all readers and writers together.
This isolated PR does not migrate the NAS, activate providers, publish GHCR or
allow a production deploy. The next change must connect v11 as *the sole*
authority across startup, scheduler and Admin, with an explicit one-way
upgrade gate and integration tests. The old rollback checklist is historical,
not a prerequisite.


## Forward-only v11 runtime and Admin activation (#552)

The runtime and Admin integration now opens existing installations using
`ForwardOnlyManagedStateStore`: the existing `managed-state.json` is converted
once to `managed-state-v11.json`, then only the v11 document is read and
written. The frozen, installation-owned historical catalog is injected into
configuration compilation, managed playlist and source administration.
Import/export fallbacks use the shared resolver so there is no silent old
state writer. Source/playlist edits persist through the atomic v11 owner store;
legacy bytes remain as inert evidence.

This is a **breaking, one-way upgrade**. After a v11 authority exists, even
if corrupted, the application fails closed instead of reading legacy state.
No old Docker image downgrade is supported. A one-off independent /data
backup before operator deployment remains prudent; no ZFS rollback project
is required. Fresh data directories now start directly with a v11 authority and **zero**
managed Spotify destinations; the image's preset catalog is still available
to Admin for explicit manual template activation. Separating catalog examples
from installed sources and the final opt-in preset UI remains planned.
This integration does not release a new image or change production TrueNAS.



## 2026-11-01 — Explicit fresh v11 source baseline

This section supersedes the historical "not connected to runtime" status above.
A fresh v11 installation now starts with **zero sources, templates and Spotify
destinations** installed. The image's bundled sources remain examples, not runtime
configuration. The existing authenticated JSON preview/Apply import workflow
is the only mechanism that installs downloaded source examples. No OAuth or
Spotify operations are required merely to view or download source presets.

A root containing arbitrary files but neither v11 authority nor a legacy
managed-state cannot be silently reinitialized. Runtime performs the initial
claim before SQLite or other runtime files are created. A previously claimed
opt-in installation identity is accepted only when it is the sole entry.
Legacy v6-v10 state retains its frozen source definitions, templates and
destinations under the forward-only migration rules. Explicit optional playlist
example-to-draft UI remains a separate outstanding #552 step.
