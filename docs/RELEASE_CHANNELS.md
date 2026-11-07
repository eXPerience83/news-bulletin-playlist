# Container release channels

The GHCR package is:

`ghcr.io/experience83/news-bulletin-playlist`

For unreleased **0.3.0b1** (forward-only schema v11), read [the v11 beta acceptance checklist](BETA_V11_ACCEPTANCE.md). The [0.2.3b1 null-catalogue recovery document](BETA_TRUENAS_RECOVERY.md) is historical and must not be used as a v11 upgrade/rollback procedure. A Python prerelease version in source or a merged PR does **not** publish a container; only separately authorized candidate/edge workflows do. Never advance stable/latest for this beta while operator acceptance (#527/#528/#562/#565) is pending.

Publication channels deliberately represent different trust levels:

`dev -> edge -> stable = latest`

| Tag | Source | Moves when | Intended use |
| --- | --- | --- | --- |
| `dev` / `dev-amd64` | Explicitly authorized PR candidate | Only after `/publish-candidate <full-head-sha>` succeeds | Deliberate pre-merge validation |
| `edge` / `edge-amd64` | Current integrated `main` | Only after **Publish edge AMD64** is manually dispatched from current `main` and succeeds | Normal experimental deployment |
| `stable` / `stable-amd64` | Newest exact stable release | Only when an exact `vMAJOR.MINOR.PATCH` tag is published | Stable deployment |
| `latest` | Same image as `stable` | Only together with `stable` | Conventional alias for latest stable |
| `candidate-pr-<PR>-<short-sha>` | One published PR candidate | Candidate-specific and never intentionally overwritten | Pre-merge audit/debug |
| `sha-<full-sha>` | Integrated `main` revision published by edge | Revision-specific and never intentionally overwritten | Integrated-build audit/debug/rollback |
| `vMAJOR.MINOR.PATCH` | One stable release image | Version-specific and never intentionally overwritten | Named stable release/rollback |
| `@sha256:<digest>` | Exact registry manifest | Never moves | Exact reproduction/rollback |

`latest` must never follow `dev` or `edge`.

The project currently publishes AMD64 images. Both the generic channel and its `*-amd64` alias point at the same AMD64 image. TrueNAS uses the architecture-explicit alias so a future multi-architecture transition cannot silently change the platform used by an existing deployment.

## Dev: reviewed PR candidates

Normal PR pushes and normal PR CI never move `dev`.

To publish a candidate, the repository owner comments on an open PR targeting `main`:

`/publish-candidate <full-40-character-head-sha>`

The low-trust `issue_comment` workflow only validates the owner command and current PR identity; it does not checkout or execute PR code and has cache access disabled. It then dispatches a separate `workflow_dispatch` worker on `main`, passing the PR number, exact HEAD SHA and authorization-comment ID. The worker independently reloads that comment and requires it to still belong to the same PR, be authored by the repository owner and contain the exact command for the authorized SHA. It also revalidates that the PR is open, targets `main`, belongs to this repository and still has the authorized HEAD.

The trusted worker then builds and smoke-tests that exact candidate in a job with no package-write permission. Its cache access is `write-only`, so it cannot restore repository caches. The tested image archive is created under `RUNNER_TEMP`, outside the PR checkout, and saved under an exact cache key bound to worker run ID, run attempt, PR number and authorized full HEAD SHA. A separate publish job uses `cache-mode: read`, has package-write permission, never checks out or executes candidate code, restores only that exact key with cache-miss failure enabled, and verifies the build-job SHA-256 before loading the image. The worker revalidates the owner comment and PR immediately before registry publication and again immediately before moving `dev`.

The transfer cache has no fallback restore keys and a rerun cannot reuse a previous attempt's entry. It uses GitHub's cache storage allowance rather than Actions artifact/Packages storage. Once a build handoff has created its exact transfer cache, the cleanup job runs even if a later publish/reporting step fails; it has cache access disabled and uses only its narrowly scoped Actions-write REST permission to delete that exact key.

`candidate-pr-<PR>-<short-sha>` is treated as an immutable audit tag by the workflow and is never overwritten. On first publication, the registry digest must resolve to the exact image that was built and smoke-tested in that run. A later retry for the same authorized PR/SHA may encounter that immutable tag even though an independent rebuild of the same source is not byte-for-byte identical. In that case the workflow reuses the existing digest only when the registry image's workflow-stamped `org.opencontainers.image.revision` and `org.opencontainers.image.version` labels still bind it to the exact authorized HEAD and PR; otherwise it fails closed. Candidate publication is serialized so two PRs cannot race the mutable `dev` channel.

Use `dev-amd64` only when deliberately validating unmerged code. Record the immutable registry digest when exact reproduction or rollback evidence matters.

## Beta: isolated immutable 0.3.0b1 candidate

The optional **Publish beta 0.3.0b1 AMD64 (immutable only)** workflow is a
**manual** `workflow_dispatch` operation on the exact current `main` SHA.
The operator must provide that full 40-character SHA, which must match both
the checked-out revision and the remote current main before building **and**
before publishing. The workflow verifies package metadata, builds and
smoke-tests the same image and publishes only:

`ghcr.io/experience83/news-bulletin-playlist:beta-0.3.0b1-<full-main-sha>`

It verifies the corresponding immutable GHCR manifest digest and prints
`ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>` in the Actions
summary. A conflicting pre-existing tag fails closed. **It never moves**
`edge`, `edge-amd64`, `stable`, `stable-amd64`, `latest`, `dev`
or `dev-amd64`, and does not deploy to TrueNAS.

This is an **available workflow only**, not authorization to run it or to
change production. Its beta image inherits forward-only v11 behavior; the
owner's specific no-backup cutover decision and the separate production
GO/NO-GO contract are recorded in [beta acceptance](BETA_V11_ACCEPTANCE.md)
and [issue #565](https://github.com/eXPerience83/news-bulletin-playlist/issues/565).

## Edge: integrated main

`edge` is published deliberately from the current `main` tip by manually running **Publish edge AMD64** in GitHub Actions. A successful run publishes:

- `edge`;
- `edge-amd64`;
- `sha-<full-main-sha>`.

The serialized publication job builds and smoke-tests the exact current `main` candidate before touching the mutable edge aliases. It verifies that the source SHA is still the current `main` tip before building, again before registry publication and immediately before mutable promotion. The workflow publishes or reuses the immutable `sha-<full-main-sha>` reference, verifies that its resolved image matches the tested local image, and promotes that exact digest to `edge` / `edge-amd64`. An older queued manual run therefore cannot move `edge` backwards after `main` has advanced.

`sha-<full-main-sha>` is the immutable integrated-build reference. The workflow creates it only when absent, reuses it only when its image content matches the tested build, and fails rather than replacing a conflicting existing tag. `edge` and `edge-amd64` are then promoted together from that exact digest and verified against it.

The checked-in TrueNAS deployment follows:

`ghcr.io/experience83/news-bulletin-playlist:edge-amd64`

This means the normal NAS instance consumes only code that has already been integrated into `main`. It remains on the previous edge image until **Publish edge AMD64** is deliberately run again.

## Stable and latest

Stable publication is intentionally a manual release decision expressed by creating an exact semantic version tag:

`vMAJOR.MINOR.PATCH`

Each numeric component follows SemVer and therefore cannot contain leading zeroes. The stable workflow also rejects a tag whose commit is not in `main` history and refuses to promote an older stable SemVer after a newer exact stable tag already exists on `main`.

The build/smoke-test job has no package-write permission. The tested image is transferred through a hashed artifact to the publication job, which revalidates that the release tag still resolves to the same source revision and that the source remains in current `main` history.

The exact `vMAJOR.MINOR.PATCH` container tag is treated as immutable. If it already exists with the same tested image, a rerun may reuse it; if it points at different image content, publication fails instead of overwriting the release.

After validation, the exact release digest is promoted to:

- `stable`;
- `stable-amd64`;
- `latest`.

The stable workflow does not rewrite the integrated `sha-<full-main-sha>` tag; that tag belongs to the edge build for that `main` revision. The versioned stable tag and immutable registry digest are the authoritative exact references for the stable build.

The three mutable stable aliases are promoted together from one exact digest and verified afterward. GHCR does not provide a transactional multi-tag update, so a registry/network failure can still interrupt an alias promotion. The workflow retries the grouped promotion once and only reports success after `stable`, `stable-amd64` and `latest` all resolve to the release digest. If a run fails during registry promotion, treat the release as incomplete and rerun the same immutable version tag after the registry issue is resolved.

Until the first stable release under this contract is published, do not treat any historical `latest` tag as a supported stable channel.

## Stable release operator checklist

Preparing a version in `pyproject.toml`, package `__version__` and `CHANGELOG.md` does **not**
publish a stable image by itself. A stable release is completed only when the exact SemVer Git tag
is created and the stable publication workflow succeeds.

For each stable release:

1. merge the release-preparation PR into `main`;
2. verify `main` contains the intended version and changelog entry;
3. create the exact Git tag `vMAJOR.MINOR.PATCH` on that release-preparation commit;
4. verify **Publish stable AMD64** succeeds for that exact tag;
5. verify GHCR exposes the immutable `:vMAJOR.MINOR.PATCH` image and that `:stable`,
   `:stable-amd64` and `:latest` resolve to the same release digest;
6. optionally create a GitHub Release for human-readable release notes. Creating the GitHub Release
   itself is informational and does not trigger the container build; the Git tag is the release
   trigger under the current workflow.

Do not consider a version stable merely because its version metadata was merged. If step 3 is
forgotten, the version exists in source history but no named stable GHCR release is published.

## Image reproduction and compatibility

> **Forward-only v11 exception:** the immutable references below reproduce an **image**, not a compatible configuration schema. Once `managed-state-v11.json` is the operative authority, there is **no supported downgrade to a pre-v11 image**, even when its digest is known. Never represent an image-only rollback or an old `managed-state.json` as v11 recovery. Preserve full independent `/data` backups and stop for operator investigation on failed cutover. Refer to [the v11 acceptance checklist](BETA_V11_ACCEPTANCE.md).

Mutable channels are convenient deployment pointers, not exact evidence. For deterministic rollback use one of the immutable references recorded by publication:

- a candidate-specific tag plus its recorded digest for pre-merge testing;
- `sha-<full-main-sha>` for an integrated edge build;
- `vMAJOR.MINOR.PATCH` for a named stable release;
- or, most exactly, `ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>`.

The `/data` dataset remains external to the image, so changing image channels does not itself erase SQLite state, Spotify authorization, or managed configuration.
