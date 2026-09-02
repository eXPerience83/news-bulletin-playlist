# Container release channels

The GHCR package is:

`ghcr.io/experience83/news-bulletin-playlist`

Publication channels deliberately represent different trust levels:

`dev -> edge -> stable = latest`

| Tag | Source | Moves when | Intended use |
| --- | --- | --- | --- |
| `dev` / `dev-amd64` | Explicitly authorized PR candidate | Only after `/publish-candidate <full-head-sha>` succeeds | Deliberate pre-merge validation |
| `edge` / `edge-amd64` | Current integrated `main` | Automatically after relevant changes reach `main` | Normal experimental deployment |
| `stable` / `stable-amd64` | Latest exact stable release | Only when an exact `vMAJOR.MINOR.PATCH` tag is published | Stable deployment |
| `latest` | Same image as `stable` | Only together with `stable` | Conventional alias for latest stable |
| `candidate-pr-<PR>-<short-sha>` | One published PR candidate | Candidate-specific | Audit/debug |
| `sha-<full-sha>` | Integrated/released revision | Revision-specific | Audit/debug/rollback |
| `@sha256:<digest>` | Exact registry manifest | Never moves | Exact reproduction/rollback |

`latest` must never follow `dev` or `edge`.

The project currently publishes AMD64 images. Both the generic channel and its `*-amd64` alias point at the same AMD64 image. TrueNAS uses the architecture-explicit alias so a future multi-architecture transition cannot silently change the platform used by an existing deployment.

## Dev: reviewed PR candidates

Normal PR pushes and normal PR CI never move `dev`.

To publish a candidate, the repository owner comments on an open PR targeting `main`:

`/publish-candidate <full-40-character-head-sha>`

The candidate workflow verifies that:

- the PR is still open;
- it targets `main`;
- its head branch belongs to this repository;
- the supplied SHA is still exactly the PR HEAD;
- the exact candidate builds and passes the hardened container health/web smoke test.

The build job has no package-write permission. It exports the tested image as an artifact. A separate publish job verifies the artifact hash, then publishes the candidate-specific tag and promotes that exact image to `dev` and `dev-amd64`. Candidate publication is serialized so two PRs cannot race the mutable `dev` channel.

Use `dev-amd64` only when deliberately validating unmerged code. Record the immutable digest when exact reproduction or rollback evidence matters.

## Edge: integrated main

Relevant image/runtime changes merged into `main` automatically publish:

- `edge`;
- `edge-amd64`;
- `sha-<full-main-sha>`.

The exact local image is built and smoke-tested before package login/promotion. The publisher then verifies that every public edge tag resolves to the same digest.

The checked-in TrueNAS deployment follows:

`ghcr.io/experience83/news-bulletin-playlist:edge-amd64`

This means the normal NAS instance consumes only code that has already been integrated into `main`.

## Stable and latest

Stable publication is intentionally a manual release decision expressed by creating an exact semantic version tag:

`vMAJOR.MINOR.PATCH`

The stable workflow rejects prerelease/non-exact tags and rejects a tag whose commit is not in `main` history. After the exact image passes the same hardened smoke test, one image is published as:

- `vMAJOR.MINOR.PATCH`;
- `stable`;
- `stable-amd64`;
- `latest`;
- `sha-<full-release-sha>`.

`stable` is the semantic deployment channel. `latest` is only an alias of the same stable digest.

Until the first stable release under this contract is published, do not treat any historical `latest` tag as a supported stable channel.

## Rollback

Mutable channels are convenient deployment pointers, not exact evidence. For an incident or deterministic rollback use the immutable digest recorded by the publishing workflow or resolve the revision-specific `sha-...` tag and pin:

`ghcr.io/experience83/news-bulletin-playlist@sha256:<digest>`

The `/data` dataset remains external to the image, so changing image channels does not itself erase SQLite state, Spotify authorization, or managed configuration.
