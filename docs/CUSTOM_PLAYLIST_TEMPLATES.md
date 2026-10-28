# Installation-owned playlist templates — staged state contract

Tracked by [#259](https://github.com/eXPerience83/news-bulletin-playlist/issues/259)
and its implementation slices [#516](https://github.com/eXPerience83/news-bulletin-playlist/issues/516),
[#517](https://github.com/eXPerience83/news-bulletin-playlist/issues/517) and
[#518](https://github.com/eXPerience83/news-bulletin-playlist/issues/518).

Implementation layers: #516 supplies the state/persistence contract; #517 adds
authenticated **My playlists → New playlist** draft create/edit/hide/delete.
#518 integrates an explicit, separately authorized Spotify activation action.
The local draft and explicit-activation flow has already been exercised on
TrueNAS (first production deployment 2026-10-14). The subsequent **custom
artwork upload** improvements are in source control until a later operator-
authorized image publication and deployment. Saving, editing or deleting
a draft **never** calls Spotify; creating a real Spotify destination remains
a separate deliberate user action.

## Built-in vs installation-owned

- Built-in playlist templates belong to `catalog.py` and the container image. Their
  IDs, default source sets and artwork are not edited through custom-template state.
- An installation-owned playlist template has a stable `custom_...` identifier,
  display name/description, validated source set, bundled or owner-uploaded cover, languages,
  optional countries, retention hours, maximum episode count, inclusive min/max
  duration, descending edition ordering and a hide/visibility flag.
- A **draft** is not a managed Spotify playlist. Existing managed playlist instances
  retain their own selected sources and eligibility policies after activation.
- Custom templates may select built-in or installation-owned sources. A hidden draft
  still owns its source references; deleting such a source must be blocked.
- New custom drafts default to the **locale-neutral custom_default** bundled JPEG,
  rather than an unrelated international or national playlist image. The new
  choice is installation-owned only and does not create a new built-in template.
  Previously saved draft and published playlist cover selections remain intact.
- Custom drafts may choose a different bundled cover or upload a JPEG/PNG via
  Admin; normalized images are stored under `/data/custom-covers` with opaque
  content-addressed IDs. Changing a managed custom playlist's local cover
  does not touch Spotify; **Apply Spotify metadata & cover** is a separate
  operator-triggered action. Arbitrary editorial filters are separate work.
  The new neutral image is built into the Docker image under
  `/opt/news-bulletin-playlist/covers/custom_default.jpg`.

## Opt-in schema v7 (no migration on boot)

Legacy installations may still have schema **v6** in
`/data/managed-state.json`. **Simply installing a newer image must not rewrite
a v6 document into v7.** Reading and ordinary saving of a v6 document leaves it
at v6, including existing Spotify destination IDs, source choices and duration ranges.
The first TrueNAS production installation already opted into **v7** by saving the
local test draft on 2026-10-14; downgrading it to a v6-only image is not safe
without restoring a compatible data snapshot.

An installation that explicitly adopted source schema **v8** keeps v8 when creating
or editing drafts; it is never downgraded to v7. See [installed-source persistence](PORTABILITY.md#installed-source-persistence-548)
for the opt-in and rollback contract. Reading v6/v7 does not opt into v8.

Schema **v7** introduces one top-level `templates` array of strictly validated
custom draft definitions. The migration is explicit: authenticated
Admin **Save local draft** atomically replaces v6 with v7 **only after**
the user deliberately creates the first custom draft. Saving/updating or
stop-managing a playlist must preserve every custom-template definition.

The v7 reader strictly rejects corrupt root state, but invalid **individual**
custom draft definitions are isolated from the effective catalogue. Their original
JSON values are retained in `quarantined_templates`, with a sanitized reason and
source-position marker. Normal `load()` does not rewrite the original file;
subsequent authenticated state changes preserve the quarantined data instead of
silently dropping it.

A managed destination referencing a quarantined custom template is excluded from
the engine's effective configuration and **receives no Spotify writes**; its
managed state and Spotify ID remain on disk. Validated independent destinations
continue to run. Admin clearly warns about quarantined drafts and disables
administrative changes for affected published playlists. Custom-source deletion
is blocked conservatively while any quarantine is present, because malformed
source references cannot be trusted.

A quarantined draft is never available for activation. Admin shows a **Discard
this local damaged draft** action only if no published playlist or pending
provisioning depends on unresolved custom state. The form requires an explicit
verified-backup checkbox, authenticated one-use CSRF and a fingerprint bound to
the raw entry, and the server rechecks all references under the state lock.
A stale/forged request or missing backup acknowledgment makes **no change**.

Discard permanently removes that **one local quarantined record** from the
installation's state file. It does not modify Spotify, active playlists, source
caches, or other stored drafts. Keep the backup for later manual recovery or
correction of the original raw data; the recovery control does not attempt to
guess valid field values. Published destinations with unresolved custom template
references remain suspended and require an independently reviewed restore.

**Quarantine and repair are implemented, but additional image rollouts still
require acceptance and a compatible rollback plan.** Never interpret quarantine
as permission to delete an existing Spotify destination or its episodes.

## Explicit Spotify activation (not a local draft save)

The Admin **My playlists** section shows a separate **Create Spotify playlist
from draft** action only when a draft is visible, not already managed, has
enabled selected sources, there is no pending provisioning to recover and Spotify
is connected. This calls the existing journaled Spotify playlist-create path.

Activation snapshots the draft defaults into the managed destination instance,
including the selected source list, duration range, retention and item count;
subsequent draft edits do not silently overwrite an already managed Spotify
playlist. A partial/uncertain provider creation must be recovered through the
existing provisioning journal rather than retried blindly. Stop-managing keeps
the Spotify destination and does not delete the local draft.

Creation is **public** by the current operator-controlled Spotify action.
For **new custom playlists**, the initial Spotify create request now includes the
validated project attribution in its description, with no extra metadata request.
Once the created destination ID is durably journaled and local managed state is
saved, Admin makes one best-effort attempt to upload the selected cover
(bundled or owner-uploaded).
A cover error cannot undo the destination, trigger duplicate creation or block
ordinary news reconciliation: Admin reports **created, cover pending** and the
existing explicit **Apply Spotify metadata & cover** action can retry later.
Saving/editing a local draft does not call Spotify. Existing image-owned built-in
activation keeps its separate manual sync policy until reviewed independently.
No automatic public README link is generated from a mere local draft.

**Production safety:** quarantine/repair are implemented and the basic
v7 draft flow has been exercised in TrueNAS, but a green PR alone is not proof
of the newer image-upload UI working with the real persistent mount. Validate
the next deployment and its rollback path independently with operator approval.

## Backup and rollback checklist

Before the **first v7 local write** on TrueNAS:

1. Stop or quiesce the application and make a verified, timestamped backup of
   `/data/managed-state.json` and the associated persistent application state.
   Do not copy OAuth tokens or sensitive diagnostic exports into GitHub issues.
2. Record the current image digest and the four real Spotify playlist destination
   IDs from authenticated Admin. **Do not replace or regenerate those destinations.**
3. Verify on the candidate image that v6 loads without rewriting and that old
   destinations are unmodified. Test v7 round-trip, restart, duplicate/collision
   guards and preservation across managed playlist saves.
4. On rollback **after** a v7 draft was created, restore the matching v6 backup
   alongside the previously deployed v6-compatible Docker image. **Rolling back
   only the image is insufficient:** older images do not understand schema v7.
   Restoring the v6 snapshot loses locally created v7 drafts; export/recreate them
   deliberately after retrying the upgrade.
5. Do not delete or recreate actual Spotify playlists as a consequence of
   rollback. The local configuration lifecycle and Spotify destination lifecycle
   are separate.

All runtime integration and eventual real Spotify activation require CI,
independent Container checks, review, and explicit operator approval. No new
deployment or Spotify action is implied by merging the persistence code.
