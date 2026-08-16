# MDBListarr

MDBListarr connects [MDBList](https://mdblist.com/) with Sonarr and Radarr. It can report library state to MDBList, synchronize MDBList collections, and optionally process the MDBList add queue. This repository is a fork of the [original MDBListarr project](https://github.com/linaspurinis/mdblistarr) by `linaspurinis`; the extended features described here belong to this fork and do not imply upstream endorsement.

## Key capabilities

- MDBList authentication, library-state upload, optional collection sync, and optional queue import for multiple Sonarr and Radarr instances.
- Authenticated administration, encrypted stored credentials, and automatically generated persistent cryptographic secrets.
- Explicit permanent/library source, On-Demand target, and queue-import roles for Arr instances.
- Sonarr and Radarr On-Demand monitoring reconciliation, persistent search tracking and retries, and conservative duplicate-file cleanup.
- A staff-only, read-only operational health dashboard.

## Installation

The container image for **this fork** is `ghcr.io/pat15312/mdblistarr:latest`. The upstream Docker Hub image is maintained separately and should not be expected to contain this fork's 2.4.0 functionality.

Create `compose.yaml`:

```yaml
services:
  mdblistarr:
    image: ghcr.io/pat15312/mdblistarr:latest
    container_name: mdblistarr
    environment:
      PORT: "5353"
    volumes:
      - ./db:/usr/src/db
    ports:
      - "5353:5353"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5353/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3
    restart: unless-stopped
```

Then run `docker compose up -d` and visit `http://localhost:5353/setup/`. Runtime secrets do not need to be placed in Compose; the container generates them on first startup.

To build from source instead:

```console
git clone https://github.com/pat15312/mdblistarr.git
cd mdblistarr
docker build -t mdblistarr:local .
```

Run that image with a persistent volume mounted at `/usr/src/db`, as in the Compose example.

## First-run setup

When no active staff administrator exists, application pages redirect to `/setup/`. Open that page only on a trusted network, claim the first administrator account, and then sign in. As an alternative for unattended initial deployment, provide `MDBLISTARR_ADMIN_PASSWORD` (or its file variant) and, optionally, `MDBLISTARR_ADMIN_USERNAME`; startup creates the first administrator but does not change an existing usable administrator.

On first container startup MDBListarr creates the Django signing key and credential-encryption key under `/usr/src/db/secrets`. Secret resolution precedence is an explicit `*_FILE`, then the direct environment variable, then the existing generated file; if none exists, startup generates the persistent file. Back up the **entire** `/usr/src/db` directory. The database and encryption key must be retained together or encrypted credentials cannot be recovered.

## Configuration

After signing in, use the application screen to connect MDBList through its OAuth device flow. A manually supplied MDBList API key remains available as a fallback when OAuth is not connected. Add Sonarr and Radarr URLs and API keys, then assign roles:

- **Permanent/library source**: contributes library state and comparison data. Reconciliation does not write to this instance.
- **On-Demand target**: permits the applicable reconciliation, monitoring, search, and cleanup writes.
- **Enable MDBList queue import**: permits queue items addressed to that instance to add media.

Roles express MDBListarr's intended use of an instance; they do not change the API key's permissions in Sonarr or Radarr. Use suitably restricted credentials and network controls where available.

A quality profile and root folder may remain blank for library-state or reconciliation-only use. Both are required when queue import is enabled because actual add requests need them.

## Standard MDBList workflow

The traditional workflow operates independently of On-Demand reconciliation:

1. At the configured UTC sync hour, MDBListarr uploads downloaded/excluded Radarr and Sonarr library state. Sync can use the first configured instance or all configured instances.
2. When **Sync Library Status** is enabled, it also adds and removes matching MDBList collection entries.
3. MDBList queue processing can add queued movies or series to their addressed Arr instance.

Queue processing defaults to **off**. It requires both the global **Enable MDBList queue processing** option and the destination instance's **Enable MDBList queue import** role. Items are skipped when the destination lacks a valid quality profile or root folder.

## On-Demand workflow

On-Demand reconciliation separates retained media from an ephemeral target:

```text
Permanent/library source
    read-only comparison and library evidence

On-Demand target
    controlled monitoring, search, and duplicate-file cleanup writes
```

This supports workflows in which the target may acquire media temporarily. NzbDAV is one possible companion in such a deployment, but it is not required and MDBListarr does not manage its filesystem. Select different source and target instances of the same Arr product and enable reconciliation explicitly. Only configured role pairs are accepted.

## Sonarr behaviour

Sonarr reconciliation matches series by TVDB ID and compares episode state. It reads the permanent/library source and applies changes only to the On-Demand target.

- Aired, scheduled episodes are eligible. Future and unscheduled episodes are unmonitored; malformed episode data makes the affected reconciliation fail closed. Season 0 specials are ignored by default.
- An eligible target episode is unmonitored when the permanent source has its file; otherwise it is monitored. A target-only series therefore monitors its eligible missing episodes.
- Season monitoring follows whether any episode in that season should be monitored. Top-level series monitoring follows whether any season is wanted.
- A permanent series counts as completely downloaded only when it has at least one relevant episode, every relevant episode has a file, and relevant data is well formed. Ignored specials, future episodes, and unscheduled episodes do not make it incomplete.
- **Search newly eligible** defaults off. When enabled, persistent candidates are created for eligible missing episodes after monitoring is confirmed; searches are not sent to the permanent source.

### Native Sonarr import-list setup

When using Sonarr native import lists to populate an On-Demand target, configure the list so Sonarr adds records without independently monitoring or searching the imported backlog:

| Setting | Value |
| --- | --- |
| Automatic Add | Enabled |
| Search for Missing Episodes | Off |
| Monitor | None |
| Monitor New Seasons | No New Seasons |
| Root folder | The intended On-Demand target root folder |
| Quality profile | The intended On-Demand target quality profile |
| Tags | Any tags required by the operator's setup |

MDBListarr is intended to decide which episodes are monitored and, when **Search newly eligible** is enabled, which newly eligible missing episodes receive explicit `EpisodeSearch` commands. Allowing the import list to monitor or search imported content independently can bypass that controlled lifecycle and trigger unwanted backlog searches. A native Sonarr import list is optional; other mechanisms may populate the target.

## Radarr behaviour

Radarr reconciliation matches movies by TMDB ID, treats the target's `isAvailable` value as authoritative, and writes only to the On-Demand target.

- A target movie is unmonitored when the permanent source has a file or the target itself has a file.
- An available movie missing from both is monitored. An unavailable movie is unmonitored.
- A target-only movie follows the same target availability/file rules, without requiring a corresponding permanent record.
- When search is enabled, eligible missing movies enter the persistent `MoviesSearch` lifecycle after monitoring is confirmed.

This reconciliation and its searches do not depend on MDBList queue processing or queue-import roles.

When using a native Radarr import list to populate the On-Demand target, use **Monitor = None** and disable **Search on Add** so Radarr does not bypass MDBListarr's monitoring and search decisions.

## Search lifecycle

Sonarr and Radarr keep durable search candidates and command records rather than treating an API request as proof of acquisition. Submission intent and candidate association are recorded before applicable `EpisodeSearch` or `MoviesSearch` commands are sent, allowing later reconciliation after uncertain responses or restarts.

Commands are polled to distinguish queued/running work, completion, genuine failure, and ambiguous or unavailable state. Completion means the Arr command finished; it does **not** prove that a release was acquired. Evidence such as a resulting file or Arr search timestamp resolves a candidate. A successfully completed search is not automatically repeated merely because the item remains missing.

Genuine failed, aborted, cancelled, or orphaned outcomes can be retried after the configured delay, up to the configured retry count (defaults: 3 retries and 30 minutes). An accepted command that disappears from Arr history without independent file or search evidence remains unavailable and fail-closed. During the missing-command grace period it is recorded as missing within grace; after that period (24 hours by default), it is recorded as missing after grace but is not automatically converted into a retryable terminal failure or retried merely because the period elapsed. The unresolved uncertainty continues to block destructive cleanup until later valid evidence resolves the command. Exhausted candidates remain visible for attention. Disabling new searches stops new submissions while maintenance still reconciles previously recorded commands and outcomes.

## Cleanup safety

Cleanup deletes duplicate **files from the On-Demand target through its Arr API**. It does not delete Sonarr series, Radarr movie records, permanent-source data, or paths directly from a filesystem or NzbDAV.

Cleanup defaults to **disabled** and, when enabled initially, **dry-run**. Both products default to a 24-hour eligibility grace period and a cap of 25 deletion attempts per reconciliation. Persistent candidates preserve the grace period across runs. Immediately before a live deletion, MDBListarr re-fetches and validates the relevant source, target, monitoring, search, and immutable file identity evidence. Changed, incomplete, malformed, ambiguous, or unavailable evidence defers or cancels deletion; destructive uncertainty can stop further deletions for that run.

- **Sonarr:** only a target episode file whose linked episodes are confirmed unmonitored permanent duplicates is eligible. Multi-episode files are handled as one immutable file candidate and all links must remain safe.
- **Radarr:** source and target files must be confirmed duplicates for the same TMDB movie. Edition metadata must be compatible; conflicting or malformed edition evidence fails closed.

Use dry-run output and the health dashboard to evaluate candidates before enabling live cleanup. Defaults are safeguards, not universal deployment recommendations.

## Operational health

`/health` is available only to staff/superusers. Rendering or refreshing it performs database reads only: it does not call MDBList, Sonarr, or Radarr. Reconciliation records its latest snapshot and counters on a best-effort basis; a health-recording failure does not change reconciliation's result.

The dashboard correlates each product's configured source/target pair with the latest run, persistent search commands/candidates, and cleanup candidates. It highlights changed or invalid configuration, overdue runs, runs that appear stale, uncertain searches, exhausted retries, and cleanup errors. Classifications are:

- **Healthy**: enabled with no current detected issue.
- **Running**: the latest matching reconciliation has started and is not overdue/stale.
- **Attention**: operator review is warranted, such as partial failure, overdue work, configuration changed since the snapshot, or uncertain lifecycle state.
- **Error**: invalid role pairing, a stale run, or a matching failed reconciliation.
- **Disabled**: reconciliation is not enabled.

## Security and reverse proxy

MDBListarr uses Django authentication and limits application administration and health views to active staff/superusers. Arr API keys and MDBList API/OAuth credentials are encrypted in the database with the persistent encryption key. This protects stored application values but is not a substitute for HTTPS, host/container security, limited API permissions, or protected backups.

For Internet-facing access, terminate HTTPS at a correctly configured trusted reverse proxy. Set secure cookies with `SESSION_COOKIE_SECURE=1` and `CSRF_COOKIE_SECURE=1`, configure allowed hosts, and list complete trusted origins such as `https://mdblistarr.example.test` in `CSRF_TRUSTED_ORIGINS`.

Forwarded proxy headers are **not trusted by default**. Prefer the explicit setting below when a trusted proxy sets and removes the corresponding header:

```console
DJANGO_SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https
```

`TRUST_PROXY_HEADERS=1` is an upstream-compatible convenience that additionally enables forwarded-host handling. Use it only when MDBListarr is reachable exclusively through a trusted proxy. Trusted-LAN HTTP can omit HTTPS settings, with the trade-off that transport is not encrypted.

## Environment variables

| Variable | Semantics |
| --- | --- |
| `PORT` | HTTP listen port; defaults to `5353`. |
| `TZ` | Django/container IANA timezone; defaults to `UTC`. Scheduled library sync still uses its configured UTC hour. |
| `DJANGO_DEBUG` | Enables Django debug mode for true-like values; defaults off. Do not enable in public deployments. |
| `DJANGO_ALLOWED_HOSTS` | Comma- or semicolon-separated Django host allowlist. Takes precedence over `ALLOWED_HOSTS`. |
| `ALLOWED_HOSTS` | Compatibility host allowlist used when `DJANGO_ALLOWED_HOSTS` is unset. The built-in list is `mdblistarr`, `localhost`, and `127.0.0.1`. |
| `CSRF_TRUSTED_ORIGINS` | Comma- or semicolon-separated complete trusted origins, including scheme. |
| `TRUST_PROXY_HEADERS` | True-like value trusts `X-Forwarded-Proto: https` and enables forwarded-host use; defaults off. |
| `DJANGO_SECURE_PROXY_SSL_HEADER` | Explicit `header,value` pair Django uses to recognize proxied HTTPS. |
| `SESSION_COOKIE_SECURE` | True-like value marks the session cookie HTTPS-only; defaults off. |
| `CSRF_COOKIE_SECURE` | True-like value marks the CSRF cookie HTTPS-only; defaults off. |
| `SESSION_COOKIE_SAMESITE` | Django session cookie SameSite value; defaults to `Lax`. |
| `CSRF_COOKIE_SAMESITE` | Django CSRF cookie SameSite value; defaults to `Lax`. |
| `DJANGO_SECRET_KEY` / `DJANGO_SECRET_KEY_FILE` | Direct or file-sourced Django signing secret; otherwise persisted automatically. |
| `MDBLISTARR_ENCRYPTION_KEY` / `MDBLISTARR_ENCRYPTION_KEY_FILE` | Direct or file-sourced Fernet key for stored credentials; otherwise persisted automatically. |
| `MDBLISTARR_ADMIN_USERNAME` | Initial bootstrap username; defaults to `admin` when password bootstrapping is used. |
| `MDBLISTARR_ADMIN_PASSWORD` / `MDBLISTARR_ADMIN_PASSWORD_FILE` | Optional direct or file-sourced password for unattended creation of the first administrator. |

For every documented `*_FILE` pair, the file value takes precedence over the direct variable. Avoid changing cryptographic secrets after data has been written.

## Upgrade and backup

Back up `/usr/src/db` before upgrades, including `db.sqlite3` and `secrets/`. Pull the current image and recreate the container while retaining the same mount:

```console
docker compose pull
docker compose up -d
```

The entrypoint runs database migrations, credential encryption maintenance, and secure administrator startup before the scheduler and web server. Keep the encryption key with its matching database. Older upstream versions do not understand this fork's encrypted credential representation, so rolling back to an upstream image is not a safe general rollback procedure; restore a compatible whole-data backup instead.

## Development and testing

Install `requirements.txt`, ensure `/usr/src/db` is writable, and run:

```console
python mdblistarr/manage.py test mdblistrr
python mdblistarr/manage.py makemigrations --check --dry-run
python mdblistarr/manage.py migrate --check
python mdblistarr/manage.py check
docker build -t mdblistarr:local .
```

GitHub Actions runs Django tests and checks, builds the image, and exercises a fresh persistent Docker volume, runtime-secret persistence across restart, `/healthz`, and `TZ` support.

## MDBListarr 2.4.0

Version 2.4.0 is a substantial backward-compatible fork feature release built on upstream 2.3.2. It brings the fork's authenticated first-run administration, encrypted credentials and persistent runtime secrets, role-aware Arr configuration, permanent/read-only sources, Sonarr On-Demand reconciliation, Radarr parity, persistent search retries, conservative duplicate-file cleanup, and operational health dashboard into one documented public release. It retains upstream 2.3.2 compatibility work for reverse proxies, path prefixes, and timezone configuration.

For older original-project history and attribution, see the [upstream repository](https://github.com/linaspurinis/mdblistarr). This repository retains the upstream license; it is not presented as the official upstream project.
