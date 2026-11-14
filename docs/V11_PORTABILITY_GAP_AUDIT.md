# 0.2.3b1 → v11: compatibility and portability acceptance gaps (#631)

**Status (updated 2026-11-10): NOT READY for production or complete owner portability.**

This is the current status of the single integration candidate PR #646, not
permission to deploy. The owner accepts a direct, forward-only cutover without
a mandatory private backup or copy of the TrueNAS dataset.

## Implemented in PR #646 (synthetic and offline evidence)

- Direct v6–v10 → v11 conversion preserves the four existing Spotify destination
  IDs, assignments, source configuration, runtime policies and inert historical
  evidence in synthetic fixtures. Real TrueNAS acceptance is **not** done.
- Historical source/template records and their full initial order are persisted
  as owner inventory. **The runtime and Admin active catalog now read this
  inventory**, not future image presets. Parser/filter/temporal capability IDs
  remain version-checked against implementations supported by the application.
- Historical cover JPEG bytes and attribution are persisted under /data with
  hash validation; corrupt persisted covers never silently use image fallbacks.
- Imported ZIP v1 creates a **local draft**. Creating a **NEW** Spotify playlist
  requires explicit confirmation and an exact matching draft. Relinking an
  **EXISTING** destination requires ownership/ID/name verification and creates
  a **paused** binding. ZIP metadata never authorizes a remote ID. Ambiguous
  creation writes remain journaled rather than blindly retried.
- The optional, durable first-boot inspection hold prevents scheduler cycles,
  provider writes and Admin mutations; read-only Admin GETs must never finalize
  pending provisioning. **The hold is not automatic.** For the intended upgrade,
  set `NEWS_PLAYLIST_STARTUP_INSPECTION=1` **before the first new-container
  boot**. Do not release the hold or start a normal cycle as part of tests.
  The offline `inspection-report` command is not a release authorization.

## Outstanding release blockers

1. **#633:** Real owner source/template edit/delete controls in Admin,
   durable CAS owner mutations, reference protection and versioned validation
   after the initial conversion. Persistence and active catalog do not equal CRUD.
2. **#638:** Lossless ZIP v2 with full template source closure, dormant duration
   exceptions, all IDs/ordering/policies and cover attribution. ZIP v1's guard
   prevents a known loss but is not a complete portable configuration.
3. **#421:** Safe scheduler shutdown, in-flight provider operations and ambiguous
   writes. A hold cannot undo an operation sent before the hold was established.
4. **#631 / #565:** Full upgrade/edit/export/import/restart acceptance, clean
   installation checks and supervised TrueNAS inspection remain pending.

**No green CI, synthetic fixture or CodeRabbit resolution alone certifies a
real production cutover.** Do not merge, publish images, modify TrueNAS or
write to Spotify without explicit owner authorization.
