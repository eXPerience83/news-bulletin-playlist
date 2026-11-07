# 0.2.3b1 → v11: compatibility and portability acceptance gaps (#631)

**Verdict (2026-11-07): NOT READY for the owner's complete portability requirement.**

A direct forward-only v6–v10→v11 conversion currently has extensive *synthetic* parity tests: it persists exact legacy state evidence and the frozen historical catalogue, preserves four remote destination IDs, and keeps stored source processing fields such as parser, temporal strategy, public release mode and collection filter. The supported implementations of those IDs must still exist in the new application.

However, three separate meanings must not be conflated:

1. **Live in-place upgrade:** the four destinations continue to exist and retain local bindings and existing SQLite/OAuth/journals when migration completes successfully; not verified on the real TrueNAS data. The first scheduler cycle may write to Spotify immediately.
2. **Config interchange:** each managed playlist can obtain a UUID by explicit Admin confirmation and export a self-contained ZIP of *assigned* sources, duration policies and selected JPEG. A separate full-source JSON is needed for sources not assigned to any exported playlist.
3. **Working restore from exports:** **not implemented.** A managed ZIP imports as a local draft and `ManagedAdminService.activate` explicitly blocks its activation. The original Spotify destination ID is deliberately absent from the ZIP and the generic relink/adopt process does not exist. OAuth, SQLite/historical episodes and provider journals are also outside this ZIP format.

Also, historical image sources/templates are frozen under installation v11 `installed_catalog`, but reconstructed into `BuiltInCatalog` and still treated as integrated: users cannot freely edit/delete them as owner-managed source/template records. This preserves legacy behavior but does **not** yet meet «no remaining installed things of series».

**Regression evidence in this PR** exercises all supported legacy schema versions across one chain: old root → v11 → prepare four export identities → export all 13 sources + four managed ZIPs → reimport in a pristine v11 root → compare full source definitions and key playlist policies/cover bytes. It intentionally asserts that imported destinations remain absent: this test documents the limit, not fulfillment of the requirement.

**Blocking work required before production upgrade with this acceptance target:**
- Ownerize historical sources/templates **without** changing source order, IDs, detector/filter behavior, disabled assignments, metadata/covers, exceptions or existing Spotify destinations; maintain pure in-place migration semantics.
- Make imported ZIP drafts usable through an explicitly validated activation flow, plus a separate guarded Spotify destination relinking operation (verified owner, read-before-write, no duplicates, ambiguous-write handling). Do not blindly trust arbitrary remote IDs in portable input.
- Define full export coverage for unassigned sources, all local drafts, and any installation-specific metadata outside playlist ZIP. Do not label portable ZIP a full history/OAuth/SQLite backup.
- Verify the exact 0.2.3b1 legacy runtime parity and provide an appropriate first-boot Spotify write gate (#630/#421). Owner declines private backup/rehearsal.

The release-only PR #629 can remain integrated, but **do not dispatch the beta workflow or deploy to TrueNAS until #631 is resolved and a separate operator decision is recorded**.
