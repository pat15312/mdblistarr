# MDBListarr

MDBListarr connects [MDBList](https://mdblist.com/) with Sonarr and Radarr. It reports library state, optionally synchronises MDBList collections and processes the MDBList add queue, and reconciles a Permanent library with an On-Demand target.

This repository is a fork of [linaspurinis/mdblistarr](https://github.com/linaspurinis/mdblistarr). Its extended behaviour belongs to this fork and does not imply upstream endorsement.

## What this fork adds

- Authenticated administration, encrypted integration credentials and persistent runtime secrets.
- Separate Permanent/library-source, On-Demand and queue-import roles.
- Sonarr and Radarr monitoring reconciliation, persistent search tracking and conservative duplicate-file cleanup.
- A staff-only Arr Health dashboard based on persisted local state.

## Quick start

The fork image is `ghcr.io/pat15312/mdblistarr:latest`, currently published for `linux/amd64`. Upstream images are maintained separately.

Create `compose.yaml`:

```yaml
services:
  mdblistarr:
    image: ghcr.io/pat15312/mdblistarr:latest
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

Run `docker compose up -d`, then open `http://localhost:5353/setup/` through a trusted connection to claim the first administrator. Secure access before exposing an unclaimed installation. For a different hostname, configure the [host allowlist and proxy settings](docs/OPERATIONS.md#environment-variables).

The container generates its cryptographic secrets automatically. Persist and back up **all of `/usr/src/db`**, including the matching encryption key. If you inject secrets externally, preserve those effective secrets separately as part of the same recovery set. See [setup and recovery](docs/OPERATIONS.md).

## Configure your workflow

Sign in, connect MDBList through OAuth device flow or its API-key fallback, then configure your Arr instances:

| Role | Purpose |
| --- | --- |
| Permanent/library source | Library and comparison evidence; read-only from reconciliation |
| On-Demand target | Controlled target-side monitoring, search and duplicate cleanup |
| MDBList queue import | Separate permission to add addressed queue items; requires a quality profile and root folder |

Queue processing also requires global opt-in. Library/reconciliation-only instances do not need add-operation settings. Traditional library sync and queue processing have [separate schedules](docs/OPERATIONS.md#schedules-and-manual-runs).

For On-Demand reconciliation, select genuinely separate source and target services of the same product and enable reconciliation explicitly. Role settings do not restrict the Arr API key itself. See [role configuration](docs/OPERATIONS.md#arr-and-mdblist-configuration).

## On-Demand behaviour at a glance

Sonarr compares aired episode state, including partially retained series, and derives season/series monitoring from episode intent. Radarr compares movie files and target availability. Both apply reconciliation writes only through the configured target.

- **Permanent sources must remain read-only from reconciliation.**
- **Search submission defaults off.** Eligible candidates can accumulate while it is off; enabling it can submit that pending work.
- **A completed search command does not prove acquisition.** Persistent state prevents uncertain or completed searches from being blindly repeated.
- **Cleanup defaults to disabled, with dry-run enabled.** It removes only validated duplicate target files through Arr APIs, after grace and revalidation. It is not a retention or general deletion engine.

Native Arr import lists should populate records without independently searching or monitoring the backlog managed by MDBListarr. Follow the [Sonarr and Radarr behaviour guide](docs/ARR_BEHAVIOUR.md), including its current implementation qualifications, before enabling searches or live cleanup.

NzbDAV, SABnzbd and other download-side components are optional external components. MDBListarr does not manage their filesystems.

## Health and troubleshooting

`/healthz` reports web responsiveness; it does not prove the scheduler or external services are healthy. The authenticated `/health` dashboard shows persisted reconciliation, search and cleanup state without contacting Arr or MDBList during rendering. See [health and troubleshooting](docs/OPERATIONS.md#operational-health).

## Upgrades and image versions

Back up the database and its effective secrets, then update the image while retaining the data mount:

```console
docker compose pull
docker compose up -d
```

Startup applies migrations and credential maintenance automatically. `latest` can advance between versioned releases. Check [release and image-tag guidance](docs/RELEASES.md) before choosing a pin, and use the [restore procedure](docs/OPERATIONS.md#upgrade-backup-and-restore) for rollback planning.

## Documentation and contributing

- [Product context](docs/PRODUCT_CONTEXT.md): historical rationale, scope and non-goals.
- [Architecture](docs/ARCHITECTURE.md): modules, models and data flow.
- [Arr behaviour](docs/ARR_BEHAVIOUR.md): monitoring, search and cleanup rules.
- [Operations](docs/OPERATIONS.md): configuration, defaults, deployment and recovery.
- [Security](docs/SECURITY.md): authentication, secrets and trust boundaries.
- [Development](docs/DEVELOPMENT.md): source builds, local setup and validation.
- [Releases](docs/RELEASES.md): versioning, publishing and upstream maintenance.
- [Agent instructions](AGENTS.md): contributor entry point for coding agents.

The declared application version lives in [mdblistarr/version](mdblistarr/version). For original-project history and attribution, see [upstream MDBListarr](https://github.com/linaspurinis/mdblistarr).
