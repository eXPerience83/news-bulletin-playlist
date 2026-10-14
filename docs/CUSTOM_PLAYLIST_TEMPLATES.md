# Installation-owned playlist templates — staged state contract

Tracked by [#259](https://github.com/eXPerience83/news-bulletin-playlist/issues/259)
and its implementation slices [#516](https://github.com/eXPerience83/news-bulletin-playlist/issues/516),
[#517](https://github.com/eXPerience83/news-bulletin-playlist/issues/517) and
[#518](https://github.com/eXPerience83/news-bulletin-playlist/issues/518).

Implementation layers: #516 supplies the state/persistence contract; #517 adds
authenticated **My playlists → New playlist** draft create/edit/hide/delete.
#518 integrates an explicit, separately authorized Spotify activation action.
These changes exist in source control, **not yet in the deployed TrueNAS image**.
Saving, editing or deleting a draft **never** calls Spotify; creating a real
Spotify destination remains a separate deliberate user action.

## Built-in vs installation-owned

- Built-in playlist templates belong to `catalog.py` and the container image. Their
  IDs, default source sets and artwork are not edited through custom-template state.
- An installation-owned playlist template has a stable `custom_...` identifier,
  display name/description, validated source set, bundled cover choice, languages,
  optional countries, retention hours, maximum episode count, inclusive min/max
  duration, descending edition ordering and a hide/visibility flag.
- A **draft** is not a managed Spotify playlist. Existing managed playlist instances
  retain their own selected sources and eligibility policies after activation.
- Custom templates may select built-in or installation-owned sources. A hidden draft
  still owns its source references; deleting such a source must be blocked.
- Early custom templates select one of the existing bundled covers. Uploading custom
  artwork and arbitrary editorial filters are separate workstreams.

## Opt-in schema v7 (no migration on boot)

The current installation state has schema **v6** in
`/data/managed-state.json`. **Simply installing a newer image must not rewrite
a v6 document into v7.** Reading and ordinary saving of a v6 document leaves it
at v6, including existing Spotify destination IDs, source choices and duration ranges.

Schema **v7** introduces one top-level `templates` array of strictly validated
custom draft definitions. The migration is explicit: authenticated
Admin **Save local draft** atomically replaces v6 with v7 **only after**
the user deliberately creates the first custom draft. Saving/updating or
stop-managing a playlist must preserve every custom-template definition.

The v7 reader rejects unknown keys, collisions with built-in IDs, duplicate
draft IDs, references to deleted or unknown sources, unrecognized cover IDs,
invalid policy ranges and malformed values. Malformed data must not authorize
a Spotify write. Isolation/repair UX for malformed custom drafts must be
completed before production activation of draft CRUD.

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
**Apply Spotify metadata & cover** is an independent explicit Admin action;
successful playlist creation does not imply cover/attribution was applied.
No automatic public README link is generated from a mere local draft.

**Production safety gate:** the remaining invalid-template quarantine/repair
contract must be reviewed before rolling out the new v7 draft UI to TrueNAS.
Do not infer production readiness from a green PR or deploy without a verified
state backup and user approval.

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
