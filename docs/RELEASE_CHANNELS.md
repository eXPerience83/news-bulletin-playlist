# Container release channels

The GHCR package is:

`ghcr.io/experience83/news-bulletin-playlist`

Publication channels deliberately represent different trust levels:

`dev -> edge -> stable = latest`

| Tag | Source | Moves when | Intended use |
| --- | --- | --- | --- |
| `dev` / `dev-amd64` | Explicitly authorized PR candidate | Only after `/publish-candidate <full-head-sha>` succeeds | Deliberate pre-merge validation |
| `edge` / `edge-amd64` | Current integrated `main` | Automatically after relevant changes reach `main` | Normal experimental deployment |
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

The candidate workflow verifies that:

- the PR is open;
- it targets `main`;
- its head branch belongs to this repository;
- the supplied SHA is exactly the PR HEAD;
- the exact candidate builds and passes the hardened container health/web smoke test.

The build job has no package-write permission. It exports the tested image as an artifact. A separate publish job verifies the artifact hash without checking out or executing candidate code. Before `dev` moves, the workflow queries the PR again and fails closed if its state, base, repository or exact HEAD SHA changed while the candidate was being built.

`candidate-pr-<PR>-<short-sha>` is treated as an immutable audit tag by the workflow. If it already exists with the same tested image, a rerun may reuse it; if it resolves to different image content, publication fails instead of overwriting it. Candidate publication is serialized so two PRs cannot race the mutable `dev` channel.

Use `dev-amd64` only when deliberately validating unmerged code. Record the immutable registry digest when exact reproduction or rollback evidence matters.

## Edge: integrated main

Relevant image/runtime changes merged into `main` automatically publish:

- `edge`;
- `edge-amd64`;
- `sha-<full-main-sha>`.

The build/smoke-test job has no package-write permission. It transfers the exact tested image through a hashed workflow artifact to a separate publishing job. Immediately before publication and again immediately before mutable promotion, the publisher verifies that the source SHA is still the current `main` tip. An older queued or manually dispatched run therefore cannot move `edge` backwards after `main` has advanced.

`sha-<full-main-sha>` is the immutable integrated-build reference. The workflow creates it only when absent, reuses it only when its image content matches the tested build, and fails rather than replacing a conflicting existing tag. `edge` and `edge-amd64` are then promoted together from that exact digest and verified against it.

The checked-in TrueNAS deployment follows:

`ghcr.io/experience83/news-bulletin-playlist:edge-amd64`

This means the normal NAS instance consumes only code that has already been integrated into `main`.

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

## Rollback

Mutable channels are convenient deployment pointers, not exact evidence. For deterministic rollback use one of the immutable references recorded by publication:

- a candidate-specific tag plus its recorded digest for pre-merge testing;
- `sha-<full-main-sha>` for an integrated edge build;
- `vMAJOR.MINOR.PATCH` for a named stable release;
- or, most exactly, `ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>`.

The `/data` dataset remains external to the image, so changing image channels does not itself erase SQLite state, Spotify authorization, or managed configuration.
