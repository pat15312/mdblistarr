# Operations

This is the canonical reference for deployment, configuration, defaults, schedules and recovery. [Arr behaviour](ARR_BEHAVIOUR.md) defines decisions and safeguards; [Security](SECURITY.md) explains trust boundaries.

## Deployment

Use this fork's `ghcr.io/pat15312/mdblistarr` image. The [README quick start](../README.md#quick-start) contains the minimal Compose deployment. Publishing currently targets `linux/amd64`; see [Releases](RELEASES.md#container-tags) before choosing an image tag.

Persist the whole `/usr/src/db` mount. No media/download filesystem mount is needed. MDBListarr communicates with Sonarr/Radarr through their APIs and remains independent of NzbDAV, SABnzbd or any other download-side component.

The current container uses Django `runserver` on port 5353 by default and starts the scheduler as a background process. The Compose example supplies a web healthcheck; the Dockerfile does not define one. A responding web process does not prove that the scheduler is running. Use one application container/scheduler per data volume; current locks are not distributed across independent containers.

## First administrator

When no **active user with both staff and superuser flags and a usable password** exists, browser application pages redirect to `/setup/`. An ordinary staff-only account is insufficient. Setup is conditional on that current account state; it is not permanently closed by a historical setup-complete flag.

Claim the first account over a trusted connection before exposing an unclaimed deployment. For unattended bootstrap, provide `MDBLISTARR_ADMIN_PASSWORD` or `MDBLISTARR_ADMIN_PASSWORD_FILE`; the username defaults to `admin` unless supplied. Bootstrap does not replace an existing usable administrator. Startup separately disables the legacy insecure `admin/admin` account.

Browser setup validates passwords and creates an active staff superuser. JSON requests to protected routes receive a setup-required response until a usable administrator exists. See [authentication details](SECURITY.md#administrative-authentication).

## Arr and MDBList configuration

Connect MDBList using OAuth device flow. A stored API key is used as fallback when no OAuth access token is connected. The API key and Arr keys are not echoed into password fields; blank edits preserve saved values where the form supports it.

Add Arr URLs and API keys, then assign roles:

- **Permanent/library source:** participates in library evidence and may be selected as a reconciliation source.
- **On-Demand target:** may be selected for permitted reconciliation writes.
- **Enable MDBList queue import:** independently permits addressed queue adds, provided global queue processing is also enabled.

Queue import requires non-blank/nonzero quality profile and root folder settings. Configure real valid Arr choices; the shared runtime check rejects missing/blank/`0` values rather than proving their continued existence in Arr. These settings may remain blank for library/reconciliation-only use.

For each product there is one configured reconciliation source/target pair, even though multiple instances may be stored for traditional workflows. Choose genuinely separate services. The current checks validate role flags and different database IDs, but allow overlapping flags and do not detect two records pointing at the same service. Preserve the [Permanent read-only boundary](ARR_BEHAVIOUR.md#shared-arr-role-model) in deployment configuration. Role flags do not restrict the API key's underlying permissions.

An On-Demand workflow needs target records populated separately. Follow the [native Sonarr](ARR_BEHAVIOUR.md#native-sonarr-import-lists) or [native Radarr](ARR_BEHAVIOUR.md#native-radarr-import-lists) import-list guidance when using those mechanisms. Enabling MDBList queue import is a separate choice that can request Arr-native searches.

## Operational defaults

These are current defaults from [models](../mdblistarr/mdblistrr/models.py), [forms](../mdblistarr/mdblistrr/forms.py), [view initialisation](../mdblistarr/mdblistrr/views.py) and [orchestration](../mdblistarr/mdblistrr/cron.py). They are safeguards and starting values, not universal deployment recommendations.

| Setting | Default | Supported UI values / meaning |
| --- | --- | --- |
| Library-source role | On | Independent per-instance flag |
| On-Demand role | Off | Explicit per-instance selection |
| Instance queue import | Off | Requires profile/root settings |
| Global MDBList queue processing | Off | Separate from reconciliation |
| Sync Library Status | Off | Enables collection additions/removals alongside library upload |
| Library sync scope | First | First library-source instance by ID for each product, or all library sources |
| UTC sync hour | Random when absent | Persisted hour from 0–23 |
| Reconciliation | Off | Independent for Sonarr and Radarr |
| Reconciliation interval | 15 minutes | 5, 15 or 30 minutes |
| Sonarr specials | Excluded | Optional season 0 inclusion |
| New search submission | Off | Discovery and maintenance can still accumulate pending candidates |
| Maximum automatic search retries | 3 | 0–10 retries after the initial accepted attempt; default allows up to four accepted attempts |
| Search retry delay | 30 minutes | 0–10080 minutes |
| Missing-command grace | 24 hours | 1–720 hours; expiry does not itself permit retry |
| Duplicate cleanup | Off | Candidate maintenance still runs with reconciliation |
| Cleanup dry-run | On | Must be disabled explicitly for live deletion |
| Cleanup grace | 24 hours | 0, 1, 6, 12, 24, 48 or 168 hours |
| Cleanup deletion-attempt cap | 25 | 1–500 attempts per reconciliation, per product |

Turning on search submission can send already-pending work. Turning on live cleanup can act on candidates whose grace elapsed while deletion was disabled or in dry-run. Review the pending workload and [lifecycle rules](ARR_BEHAVIOUR.md#search-lifecycle) first. Disabling reconciliation stops that product's maintenance as well as its new actions.

## Schedules and manual runs

| Job | Current scheduling |
| --- | --- |
| Sonarr/Radarr library uploads and optional collection sync | Hourly heartbeat, gated by the configured UTC sync hour; scheduled work sleeps for a random 0–3600 seconds before its API work |
| MDBList queue processing | Five-minute heartbeat, with random 0–36-second delay and global/destination enablement gates |
| Instance-change notification to MDBList | Fifteen-minute heartbeat; pending change logs trigger notification, with random 0–36-second delay |
| Sonarr/Radarr reconciliation | Five-minute heartbeat plus a separate configured 5/15/30-minute due-slot gate for each product |

These jobs are independent. Queue processing is not restricted to the library sync hour. `TZ` controls Django/container timezone; the library-hour comparison uses UTC. Delays and runner workload mean heartbeat times are not exact execution guarantees.

Reconciliation recovers the scheduler's due heartbeat timestamp and persists slot state in `Preferences`:

- Late execution can still service a due interval without repeatedly servicing it.
- If the product lock is busy, the pending slot is deferred.
- Once the product lock is acquired and a slot claimed, a later run failure still consumes that slot.
- Changing interval discards incompatible saved slot state.
- A successful manual forced run marks a slot serviced only when its timestamp exactly coincides with the interval boundary; other manual runs do not consume future scheduled work.

Manual **Run reconciliation now** bypasses interval gating, but still respects enablement, role validation, locks and safety checks. Manual library sync bypasses its hour gate and scheduled random delay. These are authenticated POST actions, not health-page probes.

For implementation and lock scope, see [Architecture](ARCHITECTURE.md#scheduling-and-concurrency); regressions are in [schedule tests](../mdblistarr/mdblistrr/test_reconciliation_schedule.py).

## Operational health

### Basic web health

`/healthz` is public and returns `{"status":"ok"}` without testing external APIs or scheduler progress. It is a web-responsiveness check, not integration readiness or Arr Health status.

### Arr Health

`/health` is a GET-only staff view. Rendering reads local configuration, reconciliation snapshots and lifecycle models; it does not call MDBList, Sonarr or Radarr. Recording failures do not change reconciliation results, although they can leave the displayed snapshot old or incomplete.

| Classification | Interpretation |
| --- | --- |
| Healthy | Enabled with no currently detected issue |
| Running | A recorded reconciliation is incomplete and not classified as stale/error |
| Attention | Review is warranted, for example partial failure, overdue work, changed configuration or uncertain search state |
| Error | Invalid configuration, a matching failed reconciliation or a matching stale run |
| Disabled | Reconciliation is off |

The stale/overdue threshold is `max(120 minutes, 4 × reconciliation interval)`, therefore 120 minutes for all currently supported intervals. Configuration changes can make a previous pair's snapshot unsuitable as evidence for the current pair. Lifecycle counts are scoped to a valid current target; source configuration errors are reported separately.

Pending searches and running commands represent work, not necessarily faults. Historical submitted candidates are not all active searches. The search “needs attention” total adds actionable conditions and is not a count of distinct media items. Ready dry-run cleanup and edition conflicts alone do not imply an unhealthy service.

Cleanup detail lists show at most **100 Ready and 100 Pending candidates per product**, ordered by their corresponding timestamp then ID. Sonarr episode labels are also capped at 100 per candidate. Aggregate totals include all matching rows, and truncation is indicated. Raw stored error text is not rendered in the ordinary health UI. Display metadata on older candidates may remain blank until refreshed by reconciliation.

See [health aggregation](../mdblistarr/mdblistrr/arr_health.py) and [health regressions](../mdblistarr/mdblistrr/test_arr_health.py).

## Environment variables

List settings accept comma or semicolon separators. Boolean settings below are case-insensitive, but their accepted values differ as noted.

| Variable | Current semantics |
| --- | --- |
| `PORT` | Container HTTP listen port, default `5353`; update port mapping and healthcheck URL if changed |
| `TZ` | IANA timezone for Django/container, default `UTC` |
| `DJANGO_DEBUG` | True for `1`, `true`, `yes`; default off; keep off for public deployments |
| `DJANGO_ALLOWED_HOSTS` | Host allowlist; takes precedence when non-empty |
| `ALLOWED_HOSTS` | Compatibility allowlist when the above is unset or empty; built-in fallback is `mdblistarr`, `localhost`, `127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | Complete trusted origins including scheme; default empty |
| `TRUST_PROXY_HEADERS` | True for `1`, `true`, `yes`, `on`; default off; trusts forwarded HTTPS and enables forwarded-host use |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Explicit `header,value` pair recognising proxied HTTPS; overrides the SSL mapping set by the broader flag |
| `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE` | True for `1`, `true`, `yes`; default off |
| `SESSION_COOKIE_SAMESITE`, `CSRF_COOKIE_SAMESITE` | Django SameSite values, each defaulting to `Lax` |
| `DJANGO_SECRET_KEY` / `DJANGO_SECRET_KEY_FILE` | Signing secret or explicit file source |
| `MDBLISTARR_ENCRYPTION_KEY` / `MDBLISTARR_ENCRYPTION_KEY_FILE` | Fernet credential-encryption key or explicit file source |
| `MDBLISTARR_SECRET_DIR` | Generated-secret directory; default `/usr/src/db/secrets`; does not relocate the database |
| `MDBLISTARR_ADMIN_USERNAME` / `MDBLISTARR_ADMIN_USERNAME_FILE` | Optional bootstrap username; file resolution is supported by the shared helper; defaults to `admin` |
| `MDBLISTARR_ADMIN_PASSWORD` / `MDBLISTARR_ADMIN_PASSWORD_FILE` | Optional bootstrap password; no generated administrator password |
| `RESET_DB` | Exactly `1` makes container startup delete the SQLite database before migrations; destructive, not an upgrade option |

For the two cryptographic secrets, precedence is a non-empty explicit `*_FILE` setting, non-empty direct environment value, existing generated file, then generation of a missing persistent secret. Invalid/unreadable selected files fail resolution rather than falling through. Files override direct variables for bootstrap values too, but bootstrap credentials have no generated-file stage. Explicit secrets are not copied to the generated-secret directory. See [Security](SECURITY.md#secret-storage-and-resolution).

### Advanced and development controls

| Variable | Current semantics |
| --- | --- |
| `MDBLISTARR_SETUP_LOCK_PATH` | First-admin claim lock, default `/usr/src/db/.initial-setup.lock` |
| `MDBLISTARR_RECONCILE_LOCK_PATH` | Sonarr product lock, default `/tmp/mdblistarr-sonarr-reconcile.lock` |
| `MDBLISTARR_RADARR_RECONCILE_LOCK_PATH` | Radarr product lock, default `/tmp/mdblistarr-radarr-reconcile.lock` |
| `MDBLISTARR_ALLOW_INSECURE_DEV_SECRET` | Any non-empty value, including `0`, disables automatic Django signing-secret generation/requirement in settings and allows an insecure fallback if no secret resolves; leave unset in normal deployments |

Use absolute lock paths in writable directories, consistently across cooperating processes. Changing a product lock path does not relocate the helper's separate schedule locks or provide distributed locking. The insecure development control does not supply an encryption key and does not bypass container runtime-secret initialisation.

Sources: [settings](../mdblistarr/mdblist/settings.py), [secret resolution](../mdblistarr/mdblistrr/runtime_secrets.py), [startup](../django-entrypoint.sh), [views](../mdblistarr/mdblistrr/views.py) and [cron](../mdblistarr/mdblistrr/cron.py).

## Reverse proxy and HTTPS

For Internet-facing access, terminate HTTPS at a trusted reverse proxy, configure the host allowlist and set complete trusted origins such as `https://mdblistarr.example.test`. Enable secure cookies:

```text
SESSION_COOKIE_SECURE=1
CSRF_COOKIE_SECURE=1
```

Forwarded headers are not trusted by default. If the proxy controls and sanitises the corresponding header, an explicit mapping is:

```text
DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
```

`TRUST_PROXY_HEADERS=1` also enables forwarded-host handling. Use it only when clients cannot bypass the trusted proxy. [Security](SECURITY.md#transport-and-proxy-trust) describes this boundary.

## Upgrade, backup and restore

The default persistent set includes:

```text
/usr/src/db/
├── db.sqlite3
└── secrets/
    ├── django_secret_key
    └── mdblistarr_encryption_key
```

Back up the whole data mount and the **effective matching secrets**. With explicit environment/file injection or a relocated secret directory, back up those external values through their secret-management mechanism as well. Preserve the relevant deployment configuration without exposing its secret values publicly.

Stop the application before a simple filesystem copy of SQLite and its data directory, or use a SQLite-aware consistent backup procedure. Protect backups: a complete database-plus-key backup can reveal integration credentials. Losing or replacing the encryption key makes matching encrypted credentials unrecoverable; copying only the database is insufficient.

For a normal upgrade, back up first, then:

```console
docker compose pull
docker compose up -d
```

Retain the same data mount and effective secrets. Startup runs migrations, validates existing ciphertext, encrypts legacy plaintext credentials and performs secure administrator handling. A mismatched encryption key causes credential startup validation to fail.

For restore:

1. Stop the application and restore a consistent database/data backup.
2. Restore its matching effective keys and deployment settings, including injected-secret sources.
3. Start a version compatible with that backup; permit migrations only in the intended forward direction.
4. Verify login, credential decryption, web responsiveness and subsequent reconciliation progress.

Do not pair a newer fork database with an arbitrary older upstream image as a rollback method. Use a compatible whole recovery set. `RESET_DB=1` destroys database state, including accounts and lifecycle history, and must not be used for normal upgrades. Test restores when operational continuity matters.

## Troubleshooting

Start with Arr Health, then application logs and source/target configuration. Check API reachability and scheduler progress separately from `/healthz`. `/log` displays the latest 200 stored log entries; it does not prune the database.

Uncertain command state intentionally blocks automatic assumptions, and exhausted failures retire only after validated evidence resolves the current need. Do not delete lifecycle rows merely to clear an attention state; doing so can erase duplicate-submission safeguards. Review candidates in dry-run before enabling live cleanup.

Some legacy log paths still include external payloads; review and sanitise logs before sharing them publicly. See [logging policy and implementation qualification](SECURITY.md#logging-and-observability).
