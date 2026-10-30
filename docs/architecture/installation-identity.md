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
only the separately invoked marker writer and isolated failure/concurrency tests.
Neither stage changes startup or effective catalog behavior.

Follow-up PRs under #552: gated pre-storage startup integration (only after full
legacy-migration gates), versioned frozen legacy snapshot migration and rollback
fixtures, fresh opt-in configuration and explicit preset import, and runtime/Admin
separation. The guarded marker-writing primitive alone does not change defaults.
#553 is independent empty-install round-trip acceptance; #565 remains the beta
release gate.
