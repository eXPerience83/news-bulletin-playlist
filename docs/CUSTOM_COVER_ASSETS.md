# Owner-uploaded playlist artwork: storage foundation

Issue [#527](https://github.com/eXPerience83/news-bulletin-playlist/issues/527).

This is **backend storage only**, not an Admin upload form. There is no new
network endpoint and no Spotify provider request in this slice.

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

## Pending integrations

The follow-up Admin PR must add a **dedicated bounded multipart upload
route** with authenticated one-use CSRF, and a read-only authenticated
preview route. It must reuse the existing configuration lock, select
the returned opaque asset ID only after persistent file validation, and
support uploaded images on both custom **drafts** and already managed
custom playlists. Draft edit/upload is local-only; use explicit
**Apply Spotify metadata & cover** to change a live destination.

The existing `/admin/` small `application/x-www-form-urlencoded` form
limit stays at 4096 bytes and must not be relaxed just for image content.
Reference tracking must include managed playlists, hidden drafts, pending
provisioning and quarantined entries before any garbage collection;
**no automatic asset deletion** is permitted in the storage foundation.

For rollout, test TrueNAS UID/GID 568 and persistent mount permissions,
Docker restart, immutability of existing four public products, no rate
limit regressions, manual upload and explicit post-upload metadata sync.
