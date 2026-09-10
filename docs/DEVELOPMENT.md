# Development

Read [Product context](PRODUCT_CONTEXT.md) and the relevant [Arr behaviour](ARR_BEHAVIOUR.md) before changing lifecycle logic. Preserve behaviour outside the requested scope, and report conflicts between implementation and intent rather than silently redefining either. [AGENTS.md](../AGENTS.md) is the coding-agent entry point.

## Technology and source builds

The project uses Django 6.0.x and `django-scheduled-tasks` with Django's immediate task backend. [requirements.txt](../requirements.txt) declares dependency ranges, not locked resolutions. CI validates with Python 3.12. The [Dockerfile](../Dockerfile) uses floating `python:3-slim`, installs build dependencies and `tzdata`, and installs the requirements; its Python version is not pinned to the CI interpreter.

From a checkout of this fork:

```console
docker build -t mdblistarr:local .
```

Use the [README Compose example](../README.md#quick-start) with `image: mdblistarr:local` for a normal source-built deployment. For development, use dedicated data and test Arr services; do not reuse a production database or credentials.

## Isolated container development

A disposable development container avoids changing the host's `/usr/src/db`. From the repository root, after building the image:

```console
docker volume create mdblistarr-dev-data
docker run --rm --name mdblistarr-dev -p 127.0.0.1:5353:5353 -v mdblistarr-dev-data:/usr/src/db --mount "type=bind,src=$PWD/mdblistarr,dst=/usr/src/app,readonly" mdblistarr:local
```

This mounts the source for editing on the host and keeps generated state in a separate named volume. Open `http://localhost:5353/setup/` to create a development administrator. The normal entrypoint initialises secrets, applies migrations, maintains credentials, handles setup and starts both scheduler and web server. Stop it from another terminal with `docker stop mdblistarr-dev`; the named data volume persists. Rebuild after dependency or Dockerfile changes, and restart when changes require new migrations or a scheduler reload.

## Local Python development

Use a disposable Linux environment with Python 3.12 and a dedicated writable `/usr/src/db`. The database path is fixed in settings; changing `MDBLISTARR_SECRET_DIR` only relocates secrets. Code uses POSIX file locks, so the Linux container is the most direct cross-platform development route.

The following commands are for that isolated environment, from the repository root. They create development state and must not be run against an existing production data directory:

```console
python3.12 -m venv /tmp/mdblistarr-dev-venv
. /tmp/mdblistarr-dev-venv/bin/activate
pip install -r requirements.txt
sudo mkdir -p /usr/src/db
sudo chown "$USER":"$(id -gn)" /usr/src/db
python mdblistarr/mdblistrr/runtime_secrets.py
python mdblistarr/manage.py migrate --noinput
python mdblistarr/manage.py encrypt_secrets
python mdblistarr/manage.py secure_startup
python mdblistarr/manage.py runserver 127.0.0.1:5353
```

Claim the administrator through `/setup/`, or provide the explicit bootstrap settings described in [Operations](OPERATIONS.md#first-administrator). Secret initialisation writes persistent files; it does not export variables into the parent shell, and subsequent processes resolve the same files.

In a second terminal, activate the same environment and start the scheduler from the repository root:

```console
. /tmp/mdblistarr-dev-venv/bin/activate
python mdblistarr/manage.py run_task_scheduler
```

`runserver` alone does not start scheduled jobs. Use the same effective secret settings and lock paths for both processes. Leave `MDBLISTARR_ALLOW_INSECURE_DEV_SECRET` unset for normal development; generated secrets exercise the real startup path.

## Validation by change type

### Documentation-only changes

Review all changed Markdown for consistency, check relative links and fragments, verify claims against source/tests/configuration, check [public hygiene](SECURITY.md#public-repository-hygiene), and run:

```console
git diff --check
```

Include new untracked documentation in link and whitespace checks. Documentation work does not require running migrations, bootstrapping administrators or changing application state. Explain any executable examples that were inspected but not run.

### Application changes

In the isolated development environment, the standard validation commands follow [CI](../.github/workflows/ci.yml):

```console
python mdblistarr/manage.py makemigrations --check --dry-run
python mdblistarr/manage.py migrate --noinput
python mdblistarr/manage.py migrate --check
python mdblistarr/manage.py check
SESSION_COOKIE_SECURE=1 CSRF_COOKIE_SECURE=1 python mdblistarr/manage.py check --deploy --fail-level ERROR
python mdblistarr/manage.py test mdblistrr
docker build -t mdblistarr:local .
git diff --check
```

The deploy check permits warnings below ERROR; it is not a guarantee of a warning-free or fully hardened deployment. These commands may write database, secret, cache or image state. Optional `python -m compileall -q mdblistarr/mdblistrr` also creates bytecode files and should only be used when appropriate.

Start with focused tests while iterating, then run the full application suite for merge-ready behavioural changes when practical. Report exactly what passed, failed or could not run. Do not treat a previously green CI run as validation of new code.

### Focused test map

| Area | Test modules under `mdblistrr` |
| --- | --- |
| Authentication, secrets, Sonarr decisions/cleanup and initial candidate handling | `tests` |
| Sonarr command lifecycle and retries | `test_search_lifecycle` |
| Radarr roles, migrations and configuration parity | `test_radarr_parity` |
| Radarr monitoring and manual actions | `test_radarr_reconciliation` |
| Radarr candidate/command lifecycle | `test_radarr_search` |
| Radarr destructive verification and editions | `test_radarr_cleanup` |
| Log pagination/filters, health drill-downs and display metadata | `test_frontend`, `test_media_display` |
| Health classification, detail limits and zero-network rendering | `test_arr_health` |
| Best-effort health integration and core-result preservation | `test_arr_health_reconciliation` |
| Due-slot persistence, delay and lock contention | `test_reconciliation_schedule` |
| Cleanup summaries and bounded output | `test_cleanup_logging` |
| Retained upstream compatibility | `test_upstream_sync` |

For example:

```console
python mdblistarr/manage.py test mdblistrr.test_reconciliation_schedule
```

Existing tests describe current behaviour; they do not supersede product intent or prove that untested edge cases are safe.

## Behavioural and migration review

Bug fixes should include meaningful regressions. Safety-sensitive work should demonstrate both permitted actions and blocked malformed/uncertain cases. In particular, protect Permanent-source isolation, identity/date handling, monitoring-order prerequisites, search recovery/retry lineage, exact file cleanup, grace clocks, edition compatibility and health independence.

Schema changes need migrations. Check fresh application, relevant upgrade paths, safe defaults, encrypted-value preservation and lifecycle identity/timestamps. No migration is needed without a schema change. See the [model and migration map](ARCHITECTURE.md#persistence-and-models).

Startup, persistence and secret changes need fresh-install and upgrade consideration. The current CI smoke test checks fresh-volume startup, `/setup/`, `/healthz`, generated directory/file permissions, secret persistence across container recreation, absence of generated key values from logs and timezone support. It does not exercise a real Arr acquisition or deletion. Use applicable isolated smoke validation when Docker is available.

## Implementation discipline

Prefer pure decision helpers, explicit read/decision/write orchestration and small shared primitives. Preserve product-specific semantics. Do not add transparent retries to non-idempotent search actions or direct filesystem deletion as a cleanup shortcut. Keep intent durable before uncertain side effects.

Arr reconciliation uses shared due slots; preserve late-start, lock-deferral, consumed-on-failure and manual-run semantics described in [Operations](OPERATIONS.md#schedules-and-manual-runs). Traditional jobs retain separate scheduling and should not be described as already using that helper.

Health rendering must remain network-free, and health-recording failure must preserve core results/exceptions. Output should be bounded, sanitised and suitable for public sharing. Use [Security](SECURITY.md) for the detailed trust and hygiene policy.

## Pull requests and upstream work

Describe the concrete problem and resulting behaviour, significant safety boundaries, schema/upgrade impact, validation and version impact. Include limitations and distinguish known implementation gaps from deliberate product rules. Keep temporary files, copied databases, private logs and generated artefacts out of changes.

Version changes are deliberate release decisions, not a requirement for every PR. Follow [Releases](RELEASES.md) for SemVer and the upstream integration process; useful upstream changes must preserve this fork's established contract.
