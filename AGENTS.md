# MDBListarr agent instructions

This public repository is a fork of [linaspurinis/mdblistarr](https://github.com/linaspurinis/mdblistarr).

## Read before changing

Use the documents relevant to the task:

| Document | Canonical subject |
| --- | --- |
| [Product context](docs/PRODUCT_CONTEXT.md) | Purpose, historical intent, scope, terminology and non-goals |
| [Architecture](docs/ARCHITECTURE.md) | Modules, models, persistence and execution structure |
| [Arr behaviour](docs/ARR_BEHAVIOUR.md) | Monitoring, search and duplicate-cleanup contracts |
| [Operations](docs/OPERATIONS.md) | Deployment, configuration, defaults, schedules, health and recovery |
| [Development](docs/DEVELOPMENT.md) | Local development, tests, migrations and validation |
| [Security](docs/SECURITY.md) | Authentication, trust boundaries, secrets and public hygiene |
| [Releases](docs/RELEASES.md) | Versioning, image publishing and upstream maintenance |

## Product and safety boundaries

- Sonarr and Radarr are the only Arr products in scope.
- Permanent/library-source instances provide evidence and must remain read-only from reconciliation.
- Reconciliation may apply controlled monitoring changes, explicit searches and validated duplicate-file cleanup only to On-Demand targets.
- NzbDAV, SABnzbd and similar components are external. MDBListarr must remain independent of them and must not manage their filesystems.
- Deletion rectifies confirmed On-Demand duplication. Do not broaden it into retention, pruning or general deletion without an explicit product decision.
- A submitted or completed Arr search command does not prove acquisition. Persist intent before uncertain external side effects where that prevents duplicate or ambiguous operations.
- Fail closed when required evidence is malformed, conflicting, unavailable or uncertain. Revalidate exact file identity immediately before deletion and verify absence afterwards.
- Arr Health is an observer: rendering must not call external services, and recording failure must not change reconciliation outcomes.
- Preserve Sonarr/Radarr operational parity without erasing their distinct media semantics.
- There is no standing feature roadmap. Do not invent one.

Do not simplify mature safety logic without understanding the invariant it protects.

## Change discipline

Inspect implementation, tests, migrations and relevant documentation first. Preserve behaviour outside the requested scope. Behavioural fixes need meaningful regression coverage; schema changes need migrations and consideration of fresh-install and upgrade paths. Follow [Development](docs/DEVELOPMENT.md) for applicable checks and report what was and was not validated. Documentation-only work does not require application-state changes.

Keep logs and UI output bounded and sanitised. Follow [public repository hygiene](docs/SECURITY.md#public-repository-hygiene): never commit secrets, private deployment details, personal data or development residue.

## Versioning and upstream

Follow [MAJOR.MINOR.PATCH policy](docs/RELEASES.md#versioning-policy). Not every merged PR needs a version bump; changes are made deliberately for a distinct release.

Continue reviewing useful general improvements from upstream and incorporating compatible changes. Preserve fork authentication, secret handling, source/target separation and lifecycle safeguards during integration.

## Documentation authority

Source and tests describe current implementation; product-context documents preserve durable intent. Known implementation gaps do not grant permission to weaken those principles. Investigate and report a discrepancy before deliberately changing behaviour or the documented contract.
