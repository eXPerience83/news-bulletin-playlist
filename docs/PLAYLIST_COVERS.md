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

After upgrading an existing installation:

1. use **Reconnect Spotify** once to grant the image-upload permission;
2. use **Apply Spotify metadata & cover** on the managed playlist.

That explicit action reapplies the canonical Spotify name/description (including the project link)
and then attempts the bundled cover. It is separate from **Save playlist**, so changing source
membership or pause state does not depend on Spotify metadata availability.

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
cover and the operator can reconnect/retry later.

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
