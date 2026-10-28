# Managed playlist covers

Managed playlists use curated cover assets shipped with the application image. Cover art is
playlist metadata; it is intentionally independent from source detection/catalogue refresh and
bulletin reconciliation.

## Asset layout

- Editable masters live under `assets/covers/master/`.
- Spotify-ready JPEG files live under `assets/covers/spotify/`.
- The container copies those JPEGs to `/opt/news-bulletin-playlist/covers`.
- A playlist template references its asset by `cover_id`; the runtime resolves `<cover_id>.jpg`.

The first production template, `spain_spanish_news`, uses
`assets/covers/spotify/spain_spanish_news.jpg`.
The separate, not-yet-activated long-form template, `international_spanish_long_news`,
ships matching master and JPEG artwork with an analysis-specific title and duration band.

The repository workflow `.github/workflows/render-playlist-covers.yml` renders the curated masters
into 1000×1000 Spotify-ready JPEGs. The visual family is shared across languages/countries, while
country/language labels and accents distinguish variants. Runtime image generation is deliberately
out of scope: static curated assets are simpler to review, version and reproduce.

## Spotify upload contract

Spotify custom playlist covers use `PUT /v1/playlists/{playlist_id}/images` with:

- a base64-encoded JPEG request body;
- `Content-Type: image/jpeg`;
- a maximum encoded payload of 256 KiB;
- `ugc-image-upload` plus the appropriate playlist-modification permission.

The normal playlist scopes remain the required runtime scopes. `ugc-image-upload` is requested as
an optional capability so an installation authorized before cover support can continue collecting
and reconciling bulletins without interruption.

For an activated managed playlist, use **Apply Spotify metadata & cover** to apply or reapply the
managed playlist name/base description and bundled cover. Do **not** reconnect Spotify merely
because cover support exists or because the application was upgraded. Reconnect only after an
actual Spotify authorization/permission failure indicates that the current grant is insufficient.

The explicit action is separate from **Save playlist**, so changing source membership or pause
state does not depend on Spotify metadata availability. Activation itself provisions only the
durable Spotify destination identity; it does not automatically push metadata or a cover.

### Playlist description behavior

The Admin playlist form shows a **checked, disabled** attribution indicator for both
new templates and active playlists. It is informational, not a persisted setting or
evidence that Spotify has already accepted the remote metadata. Local **Save playlist**
does not trigger Spotify metadata I/O; after activation, the operator uses the separate
**Apply Spotify metadata & cover** action, whose failures remain visible.

Managed state stores only the editable, provider-agnostic base description. On explicit Spotify
metadata synchronization, the application renders one inline attributed description:

`<base> · Made with News Bulletin Playlist · by eXPerience83 · https://github.com/eXPerience83/news-bulletin-playlist`

An empty base renders the standalone `Made with News Bulletin Playlist · by eXPerience83 · https://github.com/eXPerience83/news-bulletin-playlist` (107 characters).
The 110-character inline suffix leaves 190 base characters for a final description of at most 300.
Local descriptions retain the existing 248-character limit: existing 191–248-character bases load
and save intact. Explicit metadata sync rejects overflow locally before its PUT and explains how to
shorten the base, while still attempting cover upload independently. No truncation or schema migration
occurs. CR/LF in the base is converted to spaces only for remote rendering.

The controlled #506 LAB accepted the new single-line footer with a short base (142 total characters)
and a Unicode base of 190 characters (exactly 300 total). HTML-decoded GET readback matched exactly,
and original metadata restoration was verified by GET. The earlier #296 evidence does not establish
a universal Spotify newline rule or the historical production 400 cause.

Explicit synchronization performs one metadata PUT. HTTP 400 and other API/transport failures remain
visible to the operator; there is no retry without attribution or compensating write. Cover upload
is attempted independently, even after metadata failure. Successful metadata records
`applied_with_attribution`. No rejected description text or provider response body is retained.

Historical terminal Spanish/bilingual repository footers, the previous multiline public attribution,
the previous profile inline suffix, and the new repository inline suffix are stripped back to the base description when validated/edited.
Repeated or mixed terminal generated forms are cleaned without removing partial matches in prose.

## When covers are uploaded

A bundled cover is attempted only when an operator explicitly chooses **Apply Spotify metadata &
cover** for an activated playlist. It is not uploaded automatically during activation.

Covers are not uploaded during normal bulletin reconciliation and therefore do not add API calls to
each engine cycle.

## Failure behavior

Cover upload is best-effort. A missing/invalid local image, a missing optional Spotify scope, rate
limiting, a Spotify API failure, or a transport failure must not roll back managed state and must
never block bulletin synchronization. The playlist remains usable with Spotify's current/automatic
cover and the operator can reconnect/retry later when an actual authorization failure warrants it.

The metadata part of the explicit apply action is not best-effort: if Spotify cannot update the
playlist name/description, the action reports that failure and does not pretend the remote metadata
was synchronized. A cover failure after metadata success is still non-blocking.

## Adding a new cover

For a new country/language playlist:

1. add/review the master asset under `assets/covers/master/`;
2. render the matching JPEG under `assets/covers/spotify/`;
3. keep the rendered base64 payload within Spotify's 256 KiB limit;
4. set the playlist template's `cover_id` to the JPEG basename without `.jpg`;
5. run the cover-render workflow and normal CI/Container validation;
6. visually review the final JPEG at both full size and small-thumbnail size.

Do not use provider logos as the core identity of a managed playlist and do not put credentials or
runtime data into cover assets.

## Public release and asset rights

The repository is public. The project owner has confirmed the curated SVG masters were generated
specifically for this project with ChatGPT; their derived JPEG covers share that provenance.
The project's MIT **software** license does not grant rights to Spotify's, publishers' or other
third-party marks and content. Keep separate notices for any third-party assets.

The public repository URL is now used in generated Spotify description attribution under [#506](https://github.com/eXPerience83/news-bulletin-playlist/issues/506), following
the earlier admin-indicator work in [#490](https://github.com/eXPerience83/news-bulletin-playlist/issues/490).
The existing explicit metadata/cover synchronization contract remains unchanged; existing Spotify
descriptions are not mass-updated by an application release.

## Portable playlist export

Admin portable ZIP v1 includes the exact selected JPEG bytes, including bundled
artwork, `custom_default` and uploaded artwork. Export validates bounded local reads,
SHA256/size and JPEG decoding without recompression or Spotify downloads. Missing,
corrupt or unsafe artwork blocks the download. The archive records only available
provenance; unknown credits/licenses remain null. Inclusion does not grant artwork
redistribution rights. See [PORTABILITY.md](PORTABILITY.md#portable-playlist-zip-v1-550)
for identity preparation, compatibility and package fields.

Portable ZIP import verifies and installs JPEGs by hash without recompression,
including bundled assets from another installation. Canonical publication never
overwrites existing files; assets are synchronized before the single managed-state
commit. A failed pre-commit import may leave safe unreferenced artwork for verified
reuse. No automatic asset deletion occurs. Provenance is retained as inert draft
metadata; it is not evidence of redistribution rights. See the portability guide
for explicit v10 adoption, retry/readback and operator recovery requirements.
