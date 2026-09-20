# Production diagnostics

The production runtime uses two deliberately different diagnostic surfaces:

1. concise structured process logs on `stdout`/`stderr`, intended for TrueNAS **View Logs**;
2. a compact sanitized event history persisted in the normal application SQLite database under
   `/data`, intended for post-restart diagnosis and authenticated export.

The application does **not** write a second unbounded plaintext logfile under `/data`, does not
mount the Docker socket, and does not read Docker daemon-owned log files.

## TrueNAS / Docker container logs

TrueNAS 26 Custom Apps installed with **Install via YAML** use Docker Compose YAML. The checked-in
`deploy/truenas.yaml` therefore configures a per-container Docker logging policy instead of editing
TrueNAS' Docker daemon configuration:

```yaml
logging:
  driver: local
  options:
    max-size: "10m"
    max-file: "3"
```

Docker's `local` logging driver keeps `stdout`/`stderr` available through normal container-log
interfaces while rotating daemon-owned files. The application container is limited to three files
of at most 10 MiB each before compression. The same policy is present in the local `compose.yaml`
so development and deployment do not accidentally rely on unlimited `json-file` logs.

Do not open, copy, mount, or edit Docker's internal log files. Use TrueNAS **View Logs** or normal
Docker log commands instead.

References:

- TrueNAS Custom App YAML documentation:
  https://www.truenas.com/docs/scale/apps/installcustomappscreens/
- Docker Compose `logging` service attribute:
  https://docs.docker.com/reference/compose-file/services/#logging
- Docker `local` logging driver and rotation options:
  https://docs.docker.com/engine/logging/drivers/local/

## Structured runtime log format

Operational lines use UTC timestamps and stable fields, for example:

```text
2026-09-02T10:00:00Z level=INFO event=cycle_started component=engine cycle=cycle-...
```

Significant runtime events include cycle start/completion/failure, public-detector or official
catalogue failures, playlist reconciliation outcomes, explicit destination-preserved fail-safe
decisions, and scheduler lifecycle events. Routine successful ten-minute cycles stay summary-oriented rather
than emitting one line for every internal operation.

Raw provider response bodies and arbitrary exception strings are not copied into this structured
stream.

## Durable diagnostic history

Sanitized events are stored in the existing application SQLite database. The history has both:

- a 30-day age limit; and
- a 100,000-event emergency row limit.

The row cap is intentionally much larger than the interactive page size so ordinary production
event density can retain a meaningful multi-day/30-day window. Export metadata reports the current
event count, oldest/newest retained timestamps and whether the row cap is active.

The fields and textual values accepted by the durable event model are allow-listed. Diagnostic
persistence is fail-soft: if the event store cannot be written, the playlist engine continues and
emits only a fixed safe logging classification.

## Authenticated diagnostics UI

Open:

```text
/admin/diagnostics
```

The route uses the same administration authentication and transport rules as `/admin/`. It is not
available as a public endpoint.

The view is newest-first and supports bounded filters for:

- severity;
- source id;
- playlist id;
- recent window (1 hour, 6 hours, 24 hours, 7 days, 30 days, or all retained history);
- result count, up to 500 events.

The public `/` status page intentionally shows only success/failure classifications and counts. It
does not render raw cycle/source/playlist error strings; detailed context belongs in the
authenticated diagnostic surface.

## Export diagnostics

The diagnostics page exposes **Download sanitized diagnostics ZIP**. Export uses the same active
severity/source/playlist/time filters as the page but has its own hard capacity of **10,000 events**;
the interactive page remains bounded to **500 rows**. The export records whether its result was
truncated, so a large window cannot silently look complete.

The ZIP contains:

- `diagnostics.jsonl` — machine-readable sanitized events;
- `diagnostics.txt` — compact human-readable events;
- `status.json` — current safe cycle/source/playlist summary without raw errors;
- `runtime.json` — application version/build revision, effective retention/capacity metadata,
  export filters/truncation bounds and sanitized 24-hour Spotify request aggregates;
- `manifest.json` — bundle format/privacy metadata.

It does not contain the raw SQLite database, environment variables, Docker logs, OAuth callback
URLs, provider response bodies, unbounded episode-title/URI collections, or credential files.

Source/playlist events intentionally retain only bounded operational evidence such as detector
outcomes, refresh reasons, catalogue/request counts, coverage flags, parser/filter counters,
duration histograms, allow-listed reconciliation reasons and preservation decisions. Correlate
these with `cycle_id`, `source_id` and `playlist_id`; do not add raw provider text merely to make
an incident easier to inspect.

The diagnostic allowlist still recognizes fields emitted by released RSS/matching/planner versions
so historical rows already stored in SQLite remain readable after upgrade. Those legacy keys are
compatibility-only and are not evidence that the old runtime path is still active.

## Secret-handling contract

Automated sentinel tests cover the UI/export surfaces. These values must never appear in process
logs, durable diagnostic events, public status, authenticated diagnostics, or export bundles:

- Spotify access tokens;
- Spotify refresh tokens;
- OAuth authorization codes;
- PKCE verifiers;
- OAuth state secrets;
- administration passwords/password-file contents;
- complete sensitive callback URLs.

If a new diagnostic field needs free-form provider text, do not add it directly. Add a fixed
classification or a bounded allow-listed value instead.
