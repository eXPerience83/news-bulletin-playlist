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

Managed state stores only the editable, provider-agnostic base description. On explicit Spotify
metadata synchronization, the application first renders one bounded public attribution line:

`News Bulletin Playlist · by eXPerience83 · https://github.com/eXPerience83`

Live differential validation found that the attributed description can return Spotify HTTP 400 even
when name-only/base-description updates and cover uploads succeed. The runtime therefore retries
**only that definitive HTTP 400 metadata validation failure** once with the base description. Auth,
rate-limit, 5xx and transport failures remain visible and are not masked by the fallback.

This provider-specific behavior is not documented by Spotify's public Change Playlist Details
contract, so the project does not treat URL rejection as a portable API rule. Diagnostics record
the sanitized metadata outcome explicitly as either `applied_with_attribution` or
`applied_without_attribution`. No rejected description text or provider response body is retained.

Historical managed state that already contains a terminal `Proyecto: <repository URL>` or
`Proyecto / Project: <repository URL>` footer, as well as the current public attribution footer, is
stripped back to the base description when validated/edited so repeated synchronization cannot
duplicate attribution.

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
