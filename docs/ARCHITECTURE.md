# Architecture

This document maps the current implementation. [Product context](PRODUCT_CONTEXT.md) defines durable intent; [Arr behaviour](ARR_BEHAVIOUR.md) defines the monitoring/search/cleanup contract and records concise implementation qualifications.

## System structure

MDBListarr is a Django application with SQLite persistence, a web interface and a scheduled-task runner. Traditional MDBList integration and On-Demand reconciliation are independently configured workflows.

```text
Permanent Sonarr/Radarr -- evidence --> MDBListarr -- controlled writes --> On-Demand Sonarr/Radarr
                                           |
                                           +-- library state / collections / queue <--> MDBList
                                           |
                                           +-- SQLite configuration and lifecycle state
                                           +-- persistent runtime secrets
```

The asymmetric Arr boundary applies to reconciliation. Traditional queue imports have their own explicit permissions and may add media to their addressed instance. Download-side components are outside the application boundary.

## Module map

Paths below are relative to the repository root.

| Path | Current responsibility |
| --- | --- |
| `mdblistarr/mdblist/settings.py` | Django configuration, database path, authentication middleware, timezone and task backend |
| `mdblistarr/mdblist/urls.py` | Login/logout, basic health endpoint and application routes |
| `mdblistarr/mdblistrr/apps.py` | Imports `cron` at app readiness to register scheduled tasks |
| `mdblistarr/mdblistrr/arr.py` | SonarrAPI, RadarrAPI and MdblistAPI wrappers, including OAuth refresh and Arr HTTP operations |
| `mdblistarr/mdblistrr/connect.py` | HTTP transport, decoding, response handling, retry helpers and text sanitisation |
| `mdblistarr/mdblistrr/services.py` | Cached service construction, MDBList credential selection and Arr configuration helpers |
| `mdblistarr/mdblistrr/cron.py` | Library uploads, collection sync, queue processing, instance notifications and reconciliation orchestration |
| `mdblistarr/mdblistrr/sonarr_reconcile.py` | Episode relevance, completeness and desired episode/season/series monitoring calculations |
| `mdblistarr/mdblistrr/radarr_reconcile.py` | Movie response validation and desired monitoring calculations |
| `mdblistarr/mdblistrr/sonarr_search.py` | EpisodeSearch candidates, durable command submission and recovery/retry lifecycle |
| `mdblistarr/mdblistrr/radarr_search.py` | MoviesSearch candidates, durable command submission and recovery/retry lifecycle |
| `mdblistarr/mdblistrr/sonarr_cleanup.py` | Exact episode-file candidate lifecycle and destructive verification |
| `mdblistarr/mdblistrr/radarr_cleanup.py` | Exact movie-file candidate lifecycle, edition checks and destructive verification |
| `mdblistarr/mdblistrr/health_details.py` | Paginated, local-only search and cleanup detail views |
| `mdblistarr/mdblistrr/media_display.py` | Best-effort title backfill from existing snapshots and explicit catalogue title recovery, without lifecycle changes |
| `mdblistarr/mdblistrr/arr_health.py` | Best-effort reconciliation snapshots and local health aggregation |
| `mdblistarr/mdblistrr/reconciliation_schedule.py` | Canonical due slots and persisted scheduling state independent of health |
| `mdblistarr/mdblistrr/instance_config.py` | Shared role labels and queue-import requirement checks |
| `mdblistarr/mdblistrr/models.py` | Configuration, logs and lifecycle models |
| `mdblistarr/mdblistrr/forms.py` | Reconciliation configuration and initial administrator forms |
| `mdblistarr/mdblistrr/views.py` | Application views, MDBList/Arr instance forms, setup, manual actions and health rendering |
| `mdblistarr/mdblistrr/middleware.py` | Setup and authenticated staff-access gating |
| `mdblistarr/mdblistrr/admin_state.py` | Usable first-administrator predicate |
| `mdblistarr/mdblistrr/runtime_secrets.py` | Cryptographic-secret resolution and atomic persistent generation |
| `mdblistarr/mdblistrr/crypto.py` | Fernet encryption and decryption of integration secrets |
| `mdblistarr/mdblistrr/management/commands/encrypt_secrets.py` | Validate ciphertext and encrypt legacy plaintext credentials |
| `mdblistarr/mdblistrr/management/commands/secure_startup.py` | Disable legacy insecure admin credentials and bootstrap an administrator if needed |
| `mdblistarr/mdblistrr/log.py` | Filter and paginate persisted application log entries |

File boundaries are implementation details, not a restriction on future refactoring. See the [source directory](../mdblistarr/mdblistrr).

## Data flow

### Traditional MDBList integration

Independent scheduled jobs perform:

1. Library-source reads and downloaded/excluded-state uploads, plus optional collection synchronisation.
2. MDBList queue polling and permitted destination adds.
3. Notification of recorded instance additions, removals and name changes to MDBList.

Library selection filters for `is_library_source` before applying first/all scope. Queue processing uses its global gate, destination queue-import flag and profile/root checks. Arr roles alone do not enable it. [Operations](OPERATIONS.md#schedules-and-manual-runs) owns the schedule reference.

### Reconciliation

Each product wrapper checks enablement and schedule, acquires its product lock and claims due work. It then validates the configured source/target pairing, reads Arr state, computes desired monitoring, applies permitted target updates, maintains the search lifecycle and evaluates cleanup. Health recording is best-effort around actual run execution.

Sonarr works per target series, applying episode changes before dependent season and series changes. Radarr validates the movie snapshots and applies movie monitoring batches. The products share lifecycle concepts but retain different identity, availability and file semantics.

### Search and cleanup persistence

Reconciliation searches separate candidate eligibility from Arr command execution. A database transaction records a submitting command, candidate associations and current-command links before the external one-shot POST. Later runs validate command identity/status or independent evidence to reconcile uncertainty. Candidate and command state definitions belong in [Arr behaviour](ARR_BEHAVIOUR.md#search-lifecycle).

Cleanup candidates preserve exact target Arr file identity, eligibility timing and associated evidence. Live deletion uses the Arr API, immediate revalidation and post-delete verification. It does not traverse or delete filesystem paths directly. See [cleanup behaviour](ARR_BEHAVIOUR.md#sonarr-duplicate-file-cleanup).

## Persistence and models

The database path is fixed at `/usr/src/db/db.sqlite3` in [settings](../mdblistarr/mdblist/settings.py). The default generated secrets live under `/usr/src/db/secrets`; injected secrets may live elsewhere. Database and matching effective secrets form the recovery set described in [Operations](OPERATIONS.md#upgrade-backup-and-restore).

| Model or group | Persisted purpose and identity |
| --- | --- |
| `Preferences` | Named settings, designated encrypted MDBList secrets and JSON reconciliation schedule state |
| `SonarrInstance`, `RadarrInstance` | URLs, encrypted API keys, optional add settings and independent role flags |
| `InstanceChangeLog` | Instance added/deleted/name-change events awaiting notification |
| `Log` | Timestamped application activity; limiting displayed rows does not prune stored history |
| `SonarrEpisodeSearchCandidate`, `RadarrMovieSearchCandidate` | One candidate per target instance and target episode/movie ID; eligibility, current command, attempt count and retry timing |
| `SonarrEpisodeSearchCommand`, `RadarrMovieSearchCommand` | Durable submission attempts, Arr command identity, observed outcome and retry lineage; non-null Arr command IDs are unique per target |
| `SonarrEpisodeSearchCommandCandidate`, `RadarrMovieSearchCommandCandidate` | Explicit command/candidate relationships with submitted item IDs |
| `SonarrCleanupCandidate` | Unique target instance/episode-file ID, series/TVDB identity, linked episode keys, timestamps and status |
| `RadarrCleanupCandidate` | Unique target instance/movie-file ID, source/target movie and source file identity, TMDB ID, editions, timestamps and status |
| `ArrReconciliationStatus` | One latest snapshot per product, including source/target IDs, nullable validation evidence, counters and latest start/completion/success |

Cleanup title/year fields support display; they are not destructive evidence and metadata refresh must not reset grace or identity. Instance deletion cascades to its lifecycle records. The third-party scheduler also maintains its own run-log model, used to recover the scheduled heartbeat timestamp.

### Migration overview

The current application migration sequence is in [migrations](../mdblistarr/mdblistrr/migrations):

| Migration | Purpose |
| --- | --- |
| `0001` | Initial application schema |
| `0002` | Secret-field length and encrypted Arr-key field changes |
| `0003` | Sonarr roles and optional quality profile/root folder |
| `0004` | Radarr queue-import flag |
| `0005` | Sonarr cleanup candidates |
| `0006` | Sonarr search candidates |
| `0007` | Sonarr search attempts, commands, links and retry fields |
| `0008` | Radarr library/On-Demand roles and optional add settings |
| `0009` | Radarr search candidates, commands and links |
| `0010` | Radarr cleanup candidates |
| `0011` | Reconciliation health snapshots |
| `0012` | Cleanup candidate title/year display metadata for both products |
| `0013` | Search candidate title display metadata for both products |

Role migrations default library-source membership on and queue-import/On-Demand flags off where introduced. Existing Radarr queue-import values survive `0008`. Startup credential conversion is handled by `encrypt_secrets`, not solely by schema migrations. Due-slot state is stored in `Preferences` and did not require a separate migration.

## Scheduling and concurrency

Django uses the immediate task backend and the container starts `run_task_scheduler`. The shared due-slot helper applies to Arr reconciliation; traditional library/queue jobs retain their own scheduling logic.

Each product stores an `interval` value plus `serviced` and optional `pending` timestamps in a separate preference. Changes to interval discard incompatible slot state. Preference read/modify/write operations use separate schedule file locks. A due slot is claimed only after acquiring the product reconciliation lock. [Operations](OPERATIONS.md#schedules-and-manual-runs) explains failure, deferral and manual-run semantics.

The locks use `fcntl.flock`, with reconciliation and schedule locks normally under container-local `/tmp`. They serialize cooperating processes sharing those paths; they are not distributed locks across independent containers. The documented deployment assumes one application container/scheduler using its data volume. Do not infer multi-replica safety from persistent lifecycle state alone.

## Observability

Reconciliation uses best-effort begin/finish wrappers, including guarded warning logging. A health-recording failure must not alter the core result or exception. Rendering `/health` reads local configuration, snapshots and lifecycle records without constructing external probes. Schedule correctness does not depend on health snapshots.

Missing-name recovery is a separate staff-only, CSRF-protected POST action under the detail URL (`/titles`). GET detail rendering never calls it. It selects missing external IDs from the same paginated local detail data, performs bounded Arr catalogue GETs, and updates only blank `target_title` fields inside a per-ID transaction. Catalogue resources can have local `id=0`; exact TVDb/TMDb matching supplies display metadata only and is never reconciliation evidence. See [Operations](OPERATIONS.md#operational-health) for limits and failure reporting.

The basic `/healthz` endpoint is separate. See [Operations](OPERATIONS.md#operational-health) for endpoint meaning, classifications and display limits.

## Container and build boundary

The [entrypoint](../django-entrypoint.sh) performs secret initialisation, optional explicit database reset, migrations, credential maintenance, administrator handling, scheduler startup and web startup in that order. It currently runs Django `runserver` with a background scheduler. Operational settings belong in [Operations](OPERATIONS.md); build/test procedures in [Development](DEVELOPMENT.md); CI publishing and versioning in [Releases](RELEASES.md).
