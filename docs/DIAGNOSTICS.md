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

Raw authenticated provider/OAuth response bodies and arbitrary exception strings are not copied
into this structured stream. A #266 public-detector schema/response failure is a narrow exception:
the first unauthenticated Embed/oEmbed failure in one consecutive degraded streak may retain the
bounded response body as forensic evidence, but that body is never printed to stdout/stderr.

## Durable diagnostic history

Sanitized events are stored in the existing application SQLite database. The history has both:

- a 30-day age limit; and
- a 100,000-event emergency row limit.

The row cap is intentionally much larger than the interactive page size so ordinary production
event density can retain a meaningful multi-day/30-day window. Export metadata reports the current
event count, oldest/newest retained timestamps and whether the row cap is active.

The fields and textual values accepted by the durable event model are allow-listed. Explicit
managed-playlist metadata synchronization records only a sanitized `metadata_outcome`:
`applied_with_attribution` when Spotify accepted the rendered inline attribution. Metadata failures,
including HTTP 400, record a failed operation without a success outcome; cover results remain independent.
`applied_without_attribution` remains in the durable allow-list exclusively as a legacy label so
historical events persisted by earlier versions can still be read and exported. The current runtime
never emits that outcome; historical rows are neither migrated nor deleted.
The rejected provider body and user description text are not copied into diagnostics.

Diagnostic persistence is fail-soft: if the event store cannot be written, the playlist engine
continues and emits only a fixed safe logging classification.

## Source Audit catalogue validation failures

Rejected catalogue shapes in Source Audit and Audit All retain the public failure
`malformed_response / catalogue_shape`.
The event `source_inspection_catalogue_shape_invalid` retains
`failure_class=response_shape_error` and `phase=collection`, with these additional allowlisted
fields:

- `catalogue_reason`: a stable code from `CatalogueValidationReason`;
- `requested_offset`: the locally requested page offset, including zero;
- `item_index`: zero-based position **within that page**, only for item failures or duplicates;
- `expected_total` and `observed_total`: validated non-negative integer totals, only for
  `total_changed`.

Page reasons are `page_not_object`, `items_not_list`, `invalid_limit`, `invalid_offset`,
`invalid_total`, `total_changed`, `invalid_next`, `next_contradiction` and `impossible_paging`.
Item reasons are `item_not_object`, `invalid_episode_id`, `invalid_episode_uri`,
`episode_uri_id_mismatch`, `invalid_title`, `invalid_release_date`, `invalid_release_precision`
and `invalid_duration`. `duplicate_episode_id` identifies identity consistency failures,
including repeats across pages. No duplicate ID or rejected field value is retained.

For `catalogue_reason=item_not_object`, `item_value_kind` classifies the rejected item as
`null`, `array`, `string`, `number` or `boolean`. The fixed `other` label covers unexpected
non-JSON values from clients/fakes. Booleans are classified before numbers. This field includes
no rejected value, repr or dynamic type name; historical `item_not_object` events without it
remain readable. Current collection tolerates only literal JSON null slots as described below;
all other non-object kinds still fail closed. Classification adds no request.

The fields survive SQLite persistence and the authenticated Diagnostics ZIP export. Older events
without these fields remain readable. No SQLite or Source Audit report schema migration is needed.
The Audit All ZIP failure summary remains unchanged, and independent sources continue after a
catalogue validation failure. Classification uses the existing response only; it adds no request,
retry. Catalogue failure diagnostics contain no raw provider body, title, episode ID,
URI, arbitrary URL, header, credential or exception message.

## Tolerated unknown catalogue slots

A literal JSON null item is an unknown slot, without inferred identity or age. Source Audit succeeds
with `null_item_count`, `catalogue_complete=false`, `coverage_proven=false` and the closed warning
`catalogue_null_slots_observed` (alongside `coverage_unproven`). Audit All counts this inspection as
succeeded and includes the degraded report. The additive count defaults to zero for older in-memory
reports; report/ZIP schema versions remain unchanged. Episode evidence positions remain one-based
provider slots (`requested_offset + item_index + 1`), leaving holes where nulls occurred.

The warning event `source_catalogue_null_slots_observed` (`component=catalogue`) contains only
`null_item_count` (integer 0..200), `catalogue_complete=false` and `coverage_proven=false`.
It is emitted once per successful null-bearing fetch in audit/runtime; it is not a malformed-shape
failure. No neighboring ID/title, rejected value, repr, type name, URL, token or raw page is included.
Historical null rejection events continue reading/exporting without migration.

Runtime suppresses `api_latest_id` whenever nulls were observed, including after public enrichment.
Known rows and accepted history merge without deletion by absence. Their source trust/freshness
fields commit together in one SQLite transaction; failure rolls the entire refresh back. Every
playlist depending on an unproven source remains blocked from all reconciliation, including
additions, cap eviction, replacement and reorder. A later clean refresh recovers normal semantics.
Audit remains read-only. SQLite stays format 3 with no new tables/columns, and the null count is not
persisted as source authority.

No retry, refetch, episode lookup or extra endpoint is added. Continuing after null may reach later
pages within the existing 50 × 4 prefix; subsequent 429/duplicate/total failures still fail the
whole fetch under the existing guards. Nulls beyond the observed prefix are not counted.

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

The public `/` status page intentionally stays bounded and never renders raw response bodies. For
each source it does, however, distinguish the persisted catalogue result from the public detector
health so a source can read **Success / Detector Degraded** while last-known-good catalogue state is
being preserved. Detailed failure reason and capture metadata belong in the authenticated
diagnostics surface.

## Export all Source Audits (read-only)

From authenticated **Admin → Sources** (`/admin/sources`), select **Export all audits**.
This is separate from the **Download sanitized diagnostics ZIP** action below and from
the single-source **Audit** action. It inspects the configured built-in and custom sources
without changing source assignments, managed playlists or Spotify destinations.

1. The form submits an authenticated, CSRF-protected request and redirects immediately to
   a **Source audit export** status page. The provider inspections run in a background
   worker rather than holding the HTTP request open. The status page refreshes every
   three seconds and shows **completed / total** sources; other Admin pages and
   `/healthz` remain accessible while the worker runs.
2. When the status becomes **succeeded**, choose **Download sanitized ZIP**. A successful
   batch job can still contain individual source failures: inspect the manifest's
   `counts` and each source's `status`, not only the job status.
3. While it is running, **Cancel after current source** requests cooperative cancellation.
   It does not forcibly interrupt a provider request already in progress. A failed or
   cancelled job has no downloadable ZIP; the status page shows a safe, bounded message.
   Starting another audit while one is running returns HTTP **409 Conflict**.

Each ZIP is named `source-audits-YYYYMMDDTHHMMSSZ.zip` and contains:

- `manifest.json`: schema `news-bulletin-playlist-source-audits-v1`, generation time,
  application/build revision, ordered source summary and `counts.total`,
  `counts.attempted`, `counts.succeeded`, `counts.failed`, `counts.not_attempted`;
- `sources/<source_id>.json`: one sanitized inspection report for **each successful
  source**. Failed or unattempted sources are represented in the manifest, not as
  fabricated success reports.

The batch is **single-flight** and bounded to **20 configured sources**, **600 seconds**
and **16 MiB** of archive data. The time limit is checked between source results and
after ZIP construction; it does not kill an in-flight provider call at an arbitrary
instant. The completed ZIP is retained **only in memory**, expires **30 minutes after
completion**, is replaced by the next job and is lost on process restart. Download it
promptly. Credentials, OAuth data, raw authenticated provider bodies and the database
are not included in the archive. Like an individual audit, the batch can consume
read-only provider requests and is subject to Spotify rate limits.

Progress is recorded in durable sanitized diagnostic events with
`operation=audit_export`, `completed_sources` and `total_sources`:
`admin_source_audit_started`, `admin_source_audit_source_completed`, followed by one
of `admin_source_audit_succeeded`, `admin_source_audit_failed` or
`admin_source_audit_cancelled`. Diagnostic-store failures are fail-soft and do not
change the audit outcome.

**Operational verification:** Start an export in one browser tab; while it is running,
load `/admin/` and `/healthz` separately. Then confirm the completed ZIP opens,
the manifest counts reconcile with the per-source files and the progress/final events
appear in Diagnostics. Check normal source/playlist health independently: the audit
must not be confused with a scheduler cycle or a Spotify playlist update. Issue
[#541](https://github.com/eXPerience83/news-bulletin-playlist/issues/541) was
validated on the deployed build `0b943b7acf2f` on **2026-10-27**: all 17 sources
completed, the ZIP downloaded, and start/progress/completion diagnostics persisted
(approximately 32 seconds). That evidence does not itself establish that a
simultaneous Admin GET was timed during the production run; the non-blocking HTTP
regression is covered by CI.

## Export diagnostics

The diagnostics page exposes **Download sanitized diagnostics ZIP**. Export uses the same active
severity/source/playlist/time filters as the page but has its own hard capacity of **10,000 events**;
the interactive page remains bounded to **500 rows**. The export records whether its result was
truncated, so a large window cannot silently look complete.

The ZIP contains:

- `diagnostics.jsonl` — machine-readable bounded events;
- `diagnostics.txt` — compact human-readable events;
- `status.json` — current safe cycle/source/playlist summary without raw errors;
- `runtime.json` — application version/build revision, effective retention/capacity metadata,
  export filters/truncation bounds and sanitized 24-hour Spotify request aggregates;
- `manifest.json` — bundle format/privacy metadata;
- when #266 captures a public schema failure, one separate
  `public-responses/event-<id>-<source>-<surface>.<ext>` file containing the exact response bytes
  received by the unauthenticated detector, bounded by the existing 512 KiB response limit.

The raw public body is captured only on the first response-contract failure in one consecutive
degraded streak for that source/surface. Repeated ten-minute failures do not duplicate the body.
The normal event records only metadata such as surface, parser reason, content type, byte count,
SHA-256 and truncation status, plus a marker that a ZIP attachment exists.

The ZIP still does not contain the raw SQLite database, environment variables, Docker logs, OAuth
callback URLs, authenticated Spotify Web API/OAuth response bodies, unbounded episode-title/URI
collections, or credential files.

Source/playlist events intentionally retain bounded operational evidence such as detector outcomes,
refresh reasons, catalogue/request counts, coverage flags, parser/filter counters, duration
histograms, allow-listed reconciliation reasons and preservation decisions. The only product
diagnostic raw-body exception is the unauthenticated public Spotify response capture above, created
specifically so an unexpected Embed/oEmbed schema change can be repaired from the incident ZIP
without having to reproduce Spotify's transient response later.

The 0.1.0 persistence baseline intentionally starts with a fresh diagnostic history. Compatibility
fields that existed only to render pre-Spotify-first RSS/matcher/planner rows are no longer accepted
by the durable diagnostic model.

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

If a new diagnostic field needs free-form authenticated provider text, do not add it directly. Add
a fixed classification or a bounded allow-listed value instead. Extending the public detector raw
capture exception requires an explicit architecture/security review; it must never silently expand
to authenticated Web API/OAuth traffic.
