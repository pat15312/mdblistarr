# Releases and upstream maintenance

## Versioning policy

MDBListarr follows `MAJOR.MINOR.PATCH` semantics.

### PATCH

Increment the patch version for backward-compatible corrections and refinements such as:

- bug fixes;
- safety fixes that restore intended behaviour;
- small UI improvements;
- documentation corrections;
- operational/observability improvements;
- behavioural corrections that preserve the intended public contract.

Example:

```text
2.4.0 -> 2.4.1
```

### MINOR

Increment the minor version for meaningful backward-compatible capabilities, such as:

- a substantial new operator feature;
- a new lifecycle capability;
- a significant extension of an existing workflow;
- a major backward-compatible product enhancement.

Example:

```text
2.4.x -> 2.5.0
```

### MAJOR

Increment the major version only for deliberate breaking changes that require operator awareness or action.

Examples could include:

- incompatible configuration changes;
- intentionally incompatible persistent-data changes;
- removal of supported behaviour;
- a major product-contract change;
- breaking deployment/API expectations.

Example:

```text
2.x -> 3.0.0
```

A schema migration is not automatically a major-version change. Django migrations that safely upgrade existing deployments are normal backward-compatible development.

## Not every PR is a release

Do not bump the version for every merged pull request.

`main` should remain releasable.

Several compatible fixes or improvements may accumulate on `main` before a distinct release is cut.

Cut a release when:

- accumulated changes justify an identifiable public version;
- an important fix should be easily identifiable/pinnable;
- a meaningful capability is ready for public use;
- dependency/upstream work is substantial enough to warrant a release.

## Version source and baseline

The declared application version is stored in [mdblistarr/version](../mdblistarr/version). The 2.4.0 preparation change assembled this fork's backward-compatible capabilities on the upstream 2.3.2 baseline. Consult the version file for the current declaration rather than maintaining a separate changing version number here.

A version-file value or release-preparation commit does not prove that a matching Git tag, GitHub Release or versioned image has been published. Verify those artefacts when cutting a release or selecting a deployment pin.

## Container tags

The [current workflow](../.github/workflows/ci.yml) publishes to `ghcr.io/pat15312/mdblistarr`, for `linux/amd64` only.

| Event / reference | Configured image tags |
| --- | --- |
| Successful push to `main` | `latest` and a generated `sha-…` tag |
| Successful stable SemVer tag push `vX.Y.Z` | `X.Y.Z`, `X.Y`, `latest` and a generated `sha-…` tag |
| Successful SemVer prerelease tag push | Prerelease version and a generated `sha-…` tag; no `X.Y` or automatic `latest` |
| Pull request | Validation only; no image publication |

The workflow triggers on `v*` tags, but the SemVer rules only derive version tags from compatible version references. There is no configured major-only tag. SHA tags use the action's short format (seven characters by default); inspect published metadata for the actual tag.

`latest` advances with validated main pushes and can change between formal releases without a version bump. Stable version-tag builds also update it: the metadata action defaults to automatic `latest` generation for SemVer, independently of the explicit default-branch rule. See the action's [tag behaviour](https://github.com/docker/metadata-action/blob/v5/README.md#latest-tag) and [SemVer examples](https://github.com/docker/metadata-action/blob/v5/README.md#semver).

`X.Y` tracks a moving version line. An explicit full version identifies a release point by convention; a digest identifies the exact published image content. Verify the desired tag exists before using it. The last successful publication to a moving tag determines which build it resolves to.

## CI and release responsibilities

CI runs on pull requests, pushes to `main` and `v*` tag pushes. Its test job installs dependencies with Python 3.12, checks/applies migrations, runs Django checks and the full application suite, builds the container and exercises fresh-volume startup. The smoke test checks setup, basic web health, generated-secret permissions/persistence, absence of generated key values from logs and timezone support. See [Development](DEVELOPMENT.md#validation-by-change-type) for exact validation guidance.

Publishing requires the test job to succeed and an eligible push event. It uses `GITHUB_TOKEN` with package-write permission and Docker metadata/build actions. The image base is floating `python:3-slim`, and requirements use ranges, so source identity alone does not lock dependency/base-image contents.

The workflow does **not** bump the version, create a Git tag, create a GitHub Release or compare tag SemVer with the version file. Maintainers own those release decisions and consistency checks. Passing CI is not evidence that every expected tag is already available in GHCR.

## Release procedure

When deliberately preparing a distinct release:

1. Choose MAJOR, MINOR or PATCH based on the final public behaviour and upgrade implications.
2. Update the version file and relevant documentation, and prepare operator-focused notes.
3. Verify the intended commit's CI, migrations, applicable upgrade checks and fresh-install smoke validation.
4. Confirm public hygiene and that the version file matches the intended `vX.Y.Z` tag.
5. Tag the intended release commit and publish the tag when release execution is authorised.
6. Verify the tag workflow succeeded and GHCR contains the expected full-version and `X.Y` tags for that commit.
7. Create/verify a GitHub Release with the intended notes and upgrade information.

Release notes should explain operator-visible capabilities and fixes, configuration/default changes, schema/upgrade implications and known compatibility limits. For lifecycle or destructive changes, state the preserved safety boundaries. Avoid treating every commit as a separate release or dumping an uncurated history.

## Upstream maintenance

This fork should continue reviewing and incorporating useful compatible improvements from [linaspurinis/mdblistarr](https://github.com/linaspurinis/mdblistarr). There is no fixed calendar cadence or standing feature roadmap. Review when an upstream release or fix is relevant, dependencies/deployment change, a fork release is being prepared or divergence becomes costly.

For an upstream integration:

1. Understand the upstream behaviour and identify overlap with fork-modified areas.
2. Assess compatibility with [product intent](PRODUCT_CONTEXT.md), including authentication, secret handling, Permanent/On-Demand separation, queue-import gates, search uncertainty, cleanup and non-invasive health.
3. Incorporate useful changes, resolving conflicts deliberately; newer upstream code does not automatically take precedence.
4. Add relevant regressions, document intentional divergence and perform the applicable [development validation](DEVELOPMENT.md).
5. Decide any fork release/version impact independently of the upstream version number.

General compatibility, MDBList API, Arr API, dependency, proxy, path-prefix and timezone improvements are useful candidates when compatible. Upstream evolution does not authorise additional Arr products or weaken fork safeguards. The fork's public version describes the forked product, even when it incorporates an upstream release with a different number.

Record scoped integration decisions and already-covered changes in the [upstream review record](UPSTREAM_REVIEW.md) so subsequent reviews can reuse the comparison.

## Release-policy changes

Keep this process lightweight. Change it deliberately when established release practice justifies a revision; do not let versioning drift informally. Not every merged PR requires an immediate release or version bump.
