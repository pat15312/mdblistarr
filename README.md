# mdblistarr

Companion app for [mdblist.com](https://mdblist.com) for better Radarr and Sonarr integration.

## Docker Hub image

[linaspurinis/mdblistarr](https://hub.docker.com/r/linaspurinis/mdblistarr)

## Basics

- Connects MDBList with Radarr and Sonarr.
- Uploads your current library state back to MDBList on schedule.
- Pulls MDBList queue items and sends add requests to Radarr/Sonarr.
- Supports multiple Radarr/Sonarr instances.
- Runs as a simple Docker container with persistent DB volume.

### Basic workflow

1. Connect your MDBList account via OAuth (or enter an API key manually).
2. Add your Radarr and Sonarr instances.
3. Set quality profile and root folder mappings per instance.
4. Let scheduled sync keep MDBList and your ARR apps in sync.

## New in v2.3.0

- MDBList OAuth authentication: connect your account via the new "Connect with MDBList" button instead of copying an API key. Uses the OAuth 2.0 device authorization flow.
- API key auth still works until you connect via OAuth — once OAuth is connected, the API key is cleared and OAuth takes over.

## New in v2.2.4

- Added support for syncing library status across all configured servers

## New in v2.2.3

- Optional MDBList collection sync: enable "Sync Library Status" in the MDBList config tab to keep your MDBList collection up to date based on what is downloaded in Radarr/Sonarr.
- Configurable sync hour: choose which UTC hour of the day Radarr and Sonarr sync runs. A random hour is assigned automatically on first run to spread load across all users.
- Home page now shows last sync time and next sync estimate so you always know when to expect the next run.
- Fixed UI bug where the Radarr/Sonarr server form would reset after saving — the selected server now stays active across page reloads.

## New in v2.2.2

- Full sync now reports monitored and unmonitored items more reliably:
  - Radarr uses `hasFile` to mark downloaded vs missing.
  - Sonarr uses episode file statistics where available.
- Import list exclusions from Radarr/Sonarr are included in sync payloads.
- If a movie is already in Radarr, MDBListarr now triggers a Radarr `MoviesSearch` command instead of only logging a duplicate error.
- HTTP/JSON handling is more defensive for empty/invalid/compressed responses.

## App Configuration Screen

![image](https://github.com/user-attachments/assets/cdd58b1a-4b55-464d-84dd-55246ba6a096)

## MDBListarr

```sh
git clone --branch latest git@github.com:linaspurinis/mdblistarr.git
docker build -t mdblistarr .
docker run -e PORT=5353 -p 5353:5353 mdblistarr
```

```
services:
  mdblistarr:
    container_name: mdblistarr
    image: linaspurinis/mdblistarr:latest
    environment:
      - PORT=5353
    volumes:
      - db:/usr/src/db/
    ports:
      - '5353:5353'
volumes:
  db:
```

## Security hardening in this fork

MDBListarr now requires Django authentication for the configuration UI, logs, OAuth device-flow actions, connection tests, and state-changing endpoints. Only active staff or superuser accounts may use the application. `/healthz` and static assets remain unauthenticated.

### First-run setup and runtime secrets

The recommended home-server deployment does not require credentials or cryptographic keys in Compose. On first startup MDBListarr creates persistent runtime secret files in the existing application-data volume and then shows `/setup/` so you can claim the first administrator account in the browser. Complete this setup promptly on a trusted network because the first administrator has not yet been claimed.

Generated files live under:

- `/usr/src/db/secrets/django_secret_key`
- `/usr/src/db/secrets/mdblistarr_encryption_key`

Back up the entire `/usr/src/db` volume, not only `db.sqlite3`. Losing the generated encryption key makes encrypted application credentials unrecoverable. Deleting only `db.sqlite3` keeps the generated keys and makes the one-time setup page available again because the user table is empty.

Advanced/headless deployments may still provide explicit secrets. For each runtime secret, `_FILE` takes precedence over the direct environment variable, which takes precedence over the generated persistent file. `MDBLISTARR_ADMIN_USERNAME` plus `MDBLISTARR_ADMIN_PASSWORD` or `MDBLISTARR_ADMIN_PASSWORD_FILE` can still create the initial administrator during startup; otherwise web setup is used.

| Purpose | Variable | File alternative | Default persistent file |
| --- | --- | --- | --- |
| Initial administrator name | `MDBLISTARR_ADMIN_USERNAME` | n/a | web setup |
| Initial administrator password | `MDBLISTARR_ADMIN_PASSWORD` | `MDBLISTARR_ADMIN_PASSWORD_FILE` | web setup |
| Django signing secret | `DJANGO_SECRET_KEY` | `DJANGO_SECRET_KEY_FILE` | `/usr/src/db/secrets/django_secret_key` |
| Database secret encryption key | `MDBLISTARR_ENCRYPTION_KEY` | `MDBLISTARR_ENCRYPTION_KEY_FILE` | `/usr/src/db/secrets/mdblistarr_encryption_key` |
| Allowed hosts | `DJANGO_ALLOWED_HOSTS` | n/a | built-in LAN defaults |

A legacy `admin` account is disabled only when its stored password still verifies as the literal password `admin`; an `admin` account with a changed password is preserved.

### Encrypted credentials and migration

Sonarr API keys, Radarr API keys, the MDBList API key, and MDBList OAuth access and refresh tokens are encrypted at rest with Fernet authenticated encryption and the `mdblistarr:v1:fernet:` prefix. Existing plaintext values are migrated by `python manage.py encrypt_secrets` during container startup after migrations. The migration is idempotent and skips already encrypted values. If the encryption key is missing or wrong, startup fails rather than replacing secrets with blanks.

Back up `/usr/src/db` before upgrading. Older upstream images do not understand encrypted credentials, so rollback to upstream requires restoring the pre-migration database backup. Host-root, Docker-daemon, and running-container compromise remain outside this protection boundary.

Verify that plaintext credentials are absent without printing secrets by checking for the encrypted prefix and lengths, for example:

```sh
sqlite3 /usr/src/db/db.sqlite3 "select 'radarr', count(*) from mdblistrr_radarrinstance where apikey like 'mdblistarr:v1:fernet:%' union all select 'sonarr', count(*) from mdblistrr_sonarrinstance where apikey like 'mdblistarr:v1:fernet:%' union all select name, length(value) from mdblistrr_preferences where name in ('mdblist_apikey','mdblist_access_token','mdblist_refresh_token');"
```

### Generic Docker Compose example

```yaml
services:
  mdblistarr:
    image: ghcr.io/pat15312/mdblistarr:latest
    container_name: mdblistarr
    environment:
      PORT: "5353"
      DJANGO_ALLOWED_HOSTS: "mdblistarr,localhost,127.0.0.1,10.0.0.11,mdblistarr.lan"
    volumes:
      - ./db:/usr/src/db
    ports:
      - "5353:5353"
    healthcheck:
      test:
        - CMD
        - python
        - -c
        - "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5353/healthz', timeout=3)"
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    restart: unless-stopped
```

### Administration

Change an administrator password with `python manage.py changepassword USERNAME`. Add another staff administrator with `python manage.py createsuperuser`. Disable an administrator with `python manage.py shell -c "from django.contrib.auth import get_user_model; u=get_user_model().objects.get(username='NAME'); u.is_active=False; u.save()"`.

### Reverse proxies and HTTPS

Authentication protects the web interface; encryption protects copied database backups; HTTPS protects credentials in transit; none of these protect against host-root, Docker-daemon, or running-container compromise. Plain HTTP remains usable for a trusted home LAN, so Secure cookies and HSTS are not forced by default. For an untrusted network, terminate HTTPS at a trusted reverse proxy, set `SESSION_COOKIE_SECURE=1`, `CSRF_COOKIE_SECURE=1`, and configure `DJANGO_SECURE_PROXY_SSL_HEADER` only for proxy headers you actually trust.

## Sonarr On Demand reconciliation

MDBListarr supports three explicit Sonarr purposes so a permanent Sonarr instance can remain read-only while a separate Sonarr On Demand instance is reconciled for NzbDAV-style ephemeral viewing.

### Sonarr purposes and safety boundaries

- **Permanent library source**: MDBListarr reads series and episode state, uploads library state to MDBList, and compares episode availability during reconciliation. MDBListarr never sends add-series, monitoring, search, delete, profile, path, tag, move, rename, or file requests to this source.
- **On Demand reconciliation target**: MDBListarr writes only individual episode-monitoring changes and season-level monitoring changes. It never deletes series, episodes, or files and never changes quality profiles, root folders, series type, tags, paths, or files.
- **Queue-import capability**: MDBList queue import is separate, disabled by default, and must be explicitly enabled globally and per instance. Queue targets require a real quality profile and root folder; read-only/reconciliation-only Sonarr instances do not.

Sonarr API keys are not permission-scoped, so MDBListarr enforces the read-only and write-boundary rules in application code. Do not reuse the same Sonarr instance as both the permanent source and On Demand target for one reconciliation relationship.

### Queue processing is disabled by default

`Enable MDBList queue processing` defaults to off for new and upgraded installations. When disabled, the scheduled queue task returns without calling the MDBList queue endpoint and without sending Radarr or Sonarr add requests. To use legacy queue importing, enable the global setting and enable queue import on each desired Arr instance with a valid quality profile and root folder.

### Whole-show downloaded status

Sonarr shows are reported to MDBList as downloaded only when every relevant aired episode has `hasFile=true` in a permanent library source. Relevant episodes are regular season 1+ episodes that have already aired. Season 0 specials and future/unaired episodes are ignored by default. Enable `Include specials in completeness checks` to include specials in the same permanent-file rules. Shows with no relevant aired episodes are not reported as downloaded. Import-list exclusions remain separate from downloaded status.

This means partially retained programmes remain eligible for On Demand lists. For example, if Standard Sonarr has American Dad seasons 1-10 permanently stored but seasons 11-21 are aired and absent, MDBListarr reports the show as incomplete rather than fully downloaded, so a MDBList exclusion for Downloaded does not remove it from the On Demand list.

### On Demand reconciliation algorithm

At the configured interval, MDBListarr matches series in the On Demand target to the permanent source by TVDB ID. Episodes are matched by stable season and episode numbers from Sonarr episode data. Target episodes with a permanent source file are set unmonitored. Aired regular target episodes without a permanent source file are set monitored. A target-only series that is absent from the permanent source is treated as having no permanent files, so its aired regular episodes are eligible while future episodes and disabled specials remain unmonitored. MDBListarr then reconciles season monitoring and the top-level Sonarr series `monitored` flag from the calculated episode decisions: the series is monitored only while at least one wanted On Demand episode exists. Existing correct states are not written again.

`Search newly eligible missing episodes` is disabled by default and remains opt-in after upgrades. MDBListarr still creates persistent pending search candidates for eligible, missing, unsearched episodes while searching is disabled. When enabled later, MDBListarr submits those pending candidates once through explicit EpisodeSearch commands. Valid Sonarr `lastSearchTime` values are treated as evidence of a prior manual/external search, so those episodes are not automatically searched again. Submitted candidates are persisted locally, preventing repeated asynchronous submissions while an episode remains missing or Sonarr has not yet updated `lastSearchTime`. Permanent duplicates, episodes that already have an On Demand file, future episodes, unscheduled episodes, malformed episodes, and disabled specials are not searched. It does not run whole-series searches.

### Required Sonarr On Demand import-list setup

Configure native Sonarr MDBList import lists in the On Demand instance to:

1. Enable **Automatic Add**.
2. Set **Search for Missing Episodes** to **Off**.
3. Set **Monitor** to **None**.
4. Set **Monitor New Seasons** to **No New Seasons**.
5. Use the correct On Demand root folder and quality profile.
6. Apply the appropriate import-list tags for your setup.

This sequence is intentional: the import list safely adds the series with nothing monitored, MDBListarr compares it with permanent Sonarr, monitors only wanted episodes and seasons, monitors the top-level series only when something is wanted, persists pending search candidates for unsearched wanted missing episodes, optionally submits those candidates once, and then RSS grabs can work because the top-level series is monitored. Fully duplicated series can remain unmonitored and be handled by cleanup.

Recommended rollout for automatic On Demand acquisition:

1. Deploy with **Search newly eligible missing episodes** disabled.
2. Run and inspect monitoring reconciliation.
3. Enable **Search newly eligible missing episodes**.
4. Pending unsearched episodes are submitted once.
5. Review Sonarr queue/history and NzbDAV.

The setting remains Off by default to avoid unexpected backlog searches after upgrades.

### Scheduling, upgrade notes, and troubleshooting

Library-state sync keeps the existing configured sync-hour behaviour. On Demand reconciliation runs on the existing scheduled-task system every five minutes and internally honors the configured reconciliation interval. A file lock prevents overlapping reconciliation runs; a second invocation exits cleanly while a run is active.

Upgrades preserve encrypted API keys, existing quality profiles, root folders, authentication, and runtime secrets. Sonarr quality profile and root folder fields may be blank for read-only or reconciliation-only uses. Expected logs include counts for series inspected, complete/incomplete/no-relevant shows, exclusions, reconciliation comparisons, monitoring changes, skipped specials/future episodes, searches, and failures. Logs are sanitized and must not contain API keys or bearer tokens. Reconciliation logs distinguish matched source comparisons from target-only series and report partial failures so the next idempotent scheduled run can retry failed monitor or search batches.

### Safety-first Sonarr On Demand duplicate-file cleanup

MDBListarr can optionally evaluate Sonarr On Demand episode files for cleanup when the file is a confirmed duplicate of the permanent Sonarr source. This is a destructive feature and is disabled by default; upgrades keep cleanup disabled, dry-run enabled, a 24-hour grace period, and a 25-file deletion cap.

The only deletion reason is `permanent_duplicate`. An On Demand episode file is eligible only when every target episode linked to the same `episodeFileId` has a season/episode identity, matches the permanent source by TVDB ID plus season and episode number, the permanent episode has `hasFile=true`, the monitoring calculation explicitly chose `desired=false` because of `permanent_duplicate`, the target has `hasFile=true`, and the target episode is actually unmonitored after reconciliation writes complete. MDBListarr never treats unmonitored, future, unscheduled, malformed, disabled-special, target-only, list-excluded, or season-unmonitored state as deletion evidence.

Cleanup candidates are persistent records keyed by On Demand target instance and target `episodeFileId`. Candidates begin as `pending`, preserve `first_eligible_at` while the linked episode set is unchanged, become `ready` only after the configured grace period, then either remain dry-run would-delete entries, are deleted, are cancelled, or are marked `already_absent`. If the linked episode set changes, a cancelled candidate becomes eligible again, or Sonarr reports a replacement file ID, the grace period starts over. If the permanent source no longer has every matching file, eligibility is cancelled immediately. Malformed or uncertain data does not create, advance, or delete candidates.

Deletion is performed only against the Sonarr On Demand target through Sonarr's V3 episode-file API. MDBListarr uses bounded per-series bulk requests to `DELETE /api/v3/episodefile/bulk` with `{"episodeFileIds": [...]}`; files from different target series are never mixed in one bulk request. Immediately before deletion, MDBListarr revalidates source and target episode data, the candidate episode set, unmonitored state, `permanent_duplicate` reason, grace period, dry-run state, and deletion cap. After Sonarr accepts deletion, MDBListarr re-fetches target episodes and marks the candidate deleted only if no target episode still reports the file as active. If Sonarr returns an error but the file is already absent, the candidate is safely marked `already_absent`; if Sonarr reports success but the file remains, the candidate stays retryable with a sanitized error.

The reconciliation log includes cleanup counters: new, pending, ready, cancelled, would-delete, deleted, already-absent, deferred-by-limit, and failures. Individual sanitized candidate transition events are logged only when created, ready, cancelled, first observed as would-delete in dry run, deleted, already absent, or failed. Logs avoid API keys, auth headers, full filesystem paths, and raw sensitive Sonarr responses.

The UI provides a prominent warning and reuses the authenticated, CSRF-protected manual reconciliation action labelled "Run Sonarr reconciliation now". There is no force-delete or safety-bypass action. The action respects the existing reconciliation lock, cleanup enabled state, dry-run state, grace period, and deletion cap.

Recommended production dry-run rollout:

1. Deploy the updated application.
2. Leave cleanup disabled and confirm normal reconciliation remains healthy.
3. Enable cleanup with dry run enabled, 24-hour grace period, and maximum 25 deletions per run.
4. Review candidates and would-delete logs for at least one full grace period.
5. Confirm examples such as SpongeBob SquarePants Season 10.
6. Disable dry run to permit real deletion.
7. Confirm permanent files remain, On Demand target files disappear, target episodes remain unmonitored, target season state remains correct, and reconciliation reports zero failures.

Disabling cleanup stops DELETE calls. Dry-run can remain enabled indefinitely for candidate review. Sonarr remains responsible for removing target files and episode-file records; MDBListarr never directly deletes filesystem paths. NzbDAV backing-store or orphan cleanup may remain governed by NzbDAV's own maintenance behavior.
