# Runtime timezone

The application stores diagnostic timestamps canonically in UTC. SQLite and sanitized diagnostic ZIP exports therefore keep UTC timestamps with a `Z` suffix so logs remain portable and easy to correlate across systems.

The authenticated `/admin/diagnostics` page can render those same events in the operator's local timezone. Set the standard `TZ` environment variable to an IANA timezone name, for example:

```yaml
environment:
  TZ: "Europe/Madrid"
```

The checked-in TrueNAS deployment uses `Europe/Madrid`. If the deployment is operated elsewhere, replace it with the appropriate IANA timezone such as `Europe/London` or `America/New_York`.

When `TZ` is missing or invalid, the diagnostics UI safely falls back to UTC. Changing `TZ` affects presentation only; it does not rewrite persisted timestamps or alter diagnostic ZIP contents.

For a TrueNAS Custom App, keep `TZ` under `services.app.environment` together with the other non-secret runtime settings. After changing it, recreate/redeploy the container so the process receives the new environment value.
