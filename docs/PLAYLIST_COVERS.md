# Managed playlist covers

Managed playlists use curated cover assets shipped with the application image. Cover art is
playlist metadata; it is intentionally independent from source collection, matching and bulletin
reconciliation.

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

For an existing installation, use **Apply Spotify metadata & cover** to reapply the managed
playlist name/base description and bundled cover. Do **not** reconnect Spotify merely because cover
support exists or because the application was upgraded. Reconnect only after an actual Spotify
authorization/permission failure indicates that the current grant is insufficient.

The explicit action is separate from **Save playlist**, so changing source membership or pause
state does not depend on Spotify metadata availability.

### Playlist description behavior

The Spotify Web API receives only the editable, provider-agnostic base description. The application
does not append the repository URL to that provider field.

This choice is based on live differential validation of the current Web API path: name-only updates,
base-description updates and cover uploads succeeded, while otherwise equivalent descriptions that
contained the external project URL returned HTTP 400. Spotify's published Change Playlist Details
contract documents `description` as a string but does not document that observed URL-content
restriction, so the project treats it as provider behavior rather than a portable API guarantee.

Historical managed state that contains the old terminal `Proyecto: <repository URL>` footer is
cleaned when it is next validated/edited. The project/repository link remains available in project
documentation rather than being injected into Spotify metadata.

## When covers are uploaded

A bundled cover is attempted:

- once after a newly created Spotify playlist has been persisted safely in managed state;
- when an operator explicitly chooses **Apply Spotify metadata & cover** for an existing playlist.

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
