# Owner-uploaded playlist artwork: storage foundation

Issue [#527](https://github.com/eXPerience83/news-bulletin-playlist/issues/527).

The underlying asset store was introduced in merged PR #532. Admin now
exposes a separate authenticated upload route and read-only owner preview for
installation-owned drafts and already managed custom playlists. This local
operation **never** calls Spotify. After updating an existing public playlist's
selected cover, use **Apply Spotify metadata & cover** explicitly.

## Asset flow

- Input: 1 byte–2 MiB JPEG/PNG; minimum side 256 pixels, maximum side
  4096 pixels, maximum decoded surface 16 megapixels.
- Use Pillow to decode, honor EXIF orientation, crop centrally to a square,
  and convert to a **512 × 512 JPEG**. Strip input EXIF, GPS, PNG text, ICC
  and comments; do not persist raw uploaded bytes.
- Encode under 192 KiB JPEG so Spotify's 256 KiB **base64** payload bound
  is respected. A versioned/canonical output format avoids arbitrary
  input format reaching the provider.
- Store the final JPEG as
  `/data/custom-covers/uploaded_<64-character-lowercase-SHA256>.jpg`.
  It is created via a private 0700 directory, 0600 files, no-follow
  directory/file opens, temp-file fsync and atomic replace. The SHA256 is
  of **canonical stored bytes**, not the original upload.
- Loading verifies format markers, maximum size and SHA256 before returning
  bytes. Missing/replaced/truncated/symlinked assets fail closed, with no
  fallback image silently uploaded to Spotify.

## Admin upload and preview

- POST `/admin/owner-covers/upload`: authenticated, bounded to a 2 MiB image
  plus a small multipart framing budget, one-use CSRF, strict field names,
  transfer-encoding and MIME checks. Only `target_kind=draft|managed` and
  the matching opaque ID are accepted; custom managed playlists only.
- GET `/admin/owner-covers/uploaded_<sha256>.jpg`: authenticated, private
  no-store, returned as `image/jpeg`, opaque SHA256/path validation and
  stored file-integrity verification. Files are never served by supplied
  local path or filename.
- All state modifications happen under the existing configuration lock
  after canonical asset storage succeeds. A local state failure may leave an
  unreferenced but harmless JPEG; there is no unsafe background deletion.
- An uploaded draft image can be changed again, and a managed destination's
  local selected image can be changed without any Spotify write. For a
  published destination, explicit **Apply Spotify metadata & cover** is
  needed to synchronize the new image.

The original `/admin/` small `application/x-www-form-urlencoded` form
limit remains 4096 bytes; image bodies use only the dedicated endpoint.
Reference tracking must include managed playlists, hidden drafts, pending
provisioning and quarantined entries before any garbage collection;
**no automatic asset deletion** is permitted in the storage foundation.

For rollout, test TrueNAS UID/GID 568 and persistent mount permissions,
Docker restart, immutability of existing four public products, no rate
limit regressions, manual upload and explicit post-upload metadata sync.
