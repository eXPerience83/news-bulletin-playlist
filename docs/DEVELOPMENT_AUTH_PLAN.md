# LAN development Spotify authorization

For the current private TrueNAS development deployment, HTTPS reverse-proxy administration is deliberately deferred.

The preferred development flow is a one-time manual PKCE authorization using the registered loopback redirect URI `http://127.0.0.1:8787/callback`. The helper must persist the returned refresh credential to `/data/spotify-auth.json` using the same owner-only credential store used by the production runtime. The long-lived container can then refresh access tokens automatically without exposing an administrative login over LAN HTTP.

The read-only status page on port 8788 remains available over the trusted LAN. No state-changing `/admin/` surface is enabled in this development mode.

A future production-hardening issue covers HTTPS termination, authenticated Web UI administration and automatic browser callback handling before public/external deployment.