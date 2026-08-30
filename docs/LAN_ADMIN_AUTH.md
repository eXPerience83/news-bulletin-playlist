# LAN development Spotify authorization

The production-grade HTTPS/reverse-proxy administration design remains available and is tracked for broader exposure in #40. For the current trusted TrueNAS development LAN, the runtime also supports an explicit LAN-only administration mode so first authorization can be completed entirely in the Web UI without a shell.

## Safety boundary

LAN mode uses HTTP Basic authentication without TLS. Enable it only on a trusted private network. Do not forward port `8788` from the router, publish it through a tunnel/funnel, or expose it to an untrusted network.

The simplest no-shell TrueNAS development setup adds these values to the local Custom App YAML:

```yaml
environment:
  NEWS_PLAYLIST_ADMIN_MODE: "lan"
  NEWS_PLAYLIST_ADMIN_PASSWORD: "replace-with-a-strong-16-plus-character-password"
  SPOTIFY_CLIENT_ID: "replace-with-your-spotify-client-id"
```

The password is intentionally not present in the checked-in deployment YAML; it exists only in the private TrueNAS application configuration. `NEWS_PLAYLIST_ADMIN_PASSWORD_FILE=/data/admin-password` remains supported when owner-only file-based storage is preferred. Configure only one password source. The existing minimum-length and validation requirements still apply.

Do not configure `NEWS_PLAYLIST_EXTERNAL_URL` or `NEWS_PLAYLIST_TRUSTED_PROXY_CIDRS` in LAN mode. Those belong to the hardened HTTPS mode.

## Spotify redirect URI

Add this exact redirect URI to the Spotify developer application:

```text
http://127.0.0.1:8787/admin/spotify/callback
```

Spotify permits HTTP for explicit loopback IP addresses. The callback intentionally targets the browser machine rather than the remote NAS, so a browser connection error after approval is expected.

In LAN mode the NAS deliberately returns `404` for a direct `GET /admin/spotify/callback`. The only accepted completion path is the authenticated, CSRF-protected callback-paste form described below.

## Browser flow

1. Open `http://<truenas-host>:8788/admin/` on the trusted LAN.
2. Authenticate with username `admin` and the configured administration password.
3. Read the LAN-development warning and select **Connect Spotify**.
4. Select **Open Spotify authorization**. Spotify opens in a separate tab while the administration tab remains open.
5. Approve access in Spotify.
6. Spotify redirects the new tab to `127.0.0.1:8787`; a connection error is expected.
7. Copy the complete URL from that tab's address bar.
8. Paste it into **Complete callback URL** in the administration tab and submit.
9. The runtime validates the exact callback origin/path, OAuth state and PKCE verifier once, exchanges the code and redirects back to `/admin/`.

Only the refresh credential is written to `/data/spotify-auth.json` using the existing atomic owner-only credential store. Access tokens remain memory-only. The callback URL, authorization code and PKCE verifier are not persisted or logged.

After a restart, the scheduler obtains a new access token from the durable refresh credential automatically. If Spotify later revokes or expires the credential, the existing fail-closed reauthorization state applies.
