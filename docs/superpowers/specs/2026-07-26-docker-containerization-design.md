# Containerizing the UniFi live dashboard

**Date:** 2026-07-26
**Status:** Approved, ready for implementation planning

## Goal

Move the UniFi live dashboard off this Mac's launchd job and onto a Synology/QNAP NAS
as a Docker container, built by GitHub Actions and pulled from a private ghcr.io
registry.

## Decisions made during brainstorming

| Question | Decision |
| --- | --- |
| Where does it run? | A separate always-on host — Synology/QNAP NAS. The Mac stops hosting it. |
| How is the image built? | GitHub Actions → ghcr.io. NAS pulls; it never builds. |
| What happens to the 222 MB DB? | **Start fresh.** No migration. History rebuilds from the controller's own `stat/report` backfill. |
| What happens to launchd? | Retired, but only after the container is proven working. |
| How is access gated? | **Published to the LAN with no restriction.** Accepted risk, see below. |

## Why this project is easy to containerize

An audit of the current code found fewer obstacles than expected:

- **All timestamps are UTC** (`datetime.now(timezone.utc)` in `persist.now_iso()`,
  `db.prune_old()`, `live_server.py`). No `TZ` configuration is needed and charts
  will not shift.
- **Logging already goes to stdout** (`live_server.py:48`,
  `logging.basicConfig(..., stream=sys.stdout)`). Docker's log driver captures it
  natively. The 12 MB `live_server.log` exists only because the launchd plist
  redirects to a file.
- **No macOS-specific code.** No `darwin` checks, no homebrew paths, no `launchctl`
  calls anywhere in `live_server.py`, `unifi_lib/`, or `poll_unifi.py`.
- **`python:3.14-slim` exists** and publishes both `linux/amd64` and `linux/arm64`.

## Section 1 — Code changes

Exactly three hardcoded values block containerization. Each becomes env-overridable
**with its current value as the default**, so behaviour on the Mac is unchanged when
the variables are unset. This keeps `poll_unifi.py` and foreground debugging working.

### 1a. Bind address and port — `live_server.py:37`

Currently:

```python
HOST, PORT = "127.0.0.1", 8787
```

Becomes:

```python
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BIND_PORT", "8787"))
```

`127.0.0.1` inside a container is unreachable from the host, so the container sets
`BIND_HOST=0.0.0.0`. Keeping loopback as the *default* means nothing about the
current Mac setup changes.

### 1b. Database path — `unifi_lib/db.py:8`

Currently:

```python
DB_FILE = Path(__file__).resolve().parent.parent / "unifi_clients.db"
```

Becomes:

```python
DB_FILE = Path(os.environ.get("UNIFI_DB_PATH")
               or Path(__file__).resolve().parent.parent / "unifi_clients.db")
```

Without this the database would live inside the image and be destroyed on every
redeploy.

### 1c. Credentials — `unifi_lib/fetch.py:19` and `load_config()`

`SETTINGS_FILE` points at `../../.claude/settings.local.json`, resolving to
`/Users/hector/Projects/.claude/settings.local.json`. That path does not exist in a
container, and `read_text()` currently raises `FileNotFoundError` if it is missing.

`load_config()` already looks up env-var *names* out of that JSON's `env` block, so
the change is to read the real process environment first and fall back to the file:

- Read `UNIFI_NETWORK_HOST` / `UNIFI_HOST`, `UNIFI_NETWORK_USERNAME` /
  `UNIFI_USERNAME`, `UNIFI_NETWORK_PASSWORD` / `UNIFI_PASSWORD`, `UNIFI_NETWORK_PORT`,
  `UNIFI_NETWORK_SITE`, `UNIFI_NETWORK_VERIFY_SSL` from `os.environ` first.
- Fall back to the JSON file for any value not present in the environment.
- **Tolerate the JSON file being absent** rather than raising.
- Keep the existing `RuntimeError` when host/username/password end up unset from
  either source, so a misconfigured container fails loudly at startup.

No other backend restructuring is required.

## Section 2 — Image, ports, and the data volume

### Base image and dependencies

`python:3.14-slim`, running as a non-root user (uid/gid 1000). Dependencies are
pinned in a new `requirements.txt` to exactly what is running today:

```text
unifi-core==0.4.20
aiounifi==92
aiohttp==3.14.3
```

These versions were read from the live `uv` environment, not guessed.

The image copies `live_server.py`, `unifi_lib/`, `static/`, and `poll_unifi.py`.
Nothing else.

### Ports

Two distinct ports, both configurable:

| Variable | Scope | Default | Purpose |
| --- | --- | --- | --- |
| `BIND_PORT` | inside the container | `8787` | what aiohttp listens on |
| `HOST_PORT` | NAS side, compose | `8787` | what you browse to |

```yaml
ports:
  - "${HOST_PORT:-8787}:${BIND_PORT:-8787}"
```

Normal use changes `HOST_PORT` only; `BIND_PORT` matters solely if something in the
container namespace collides. Both live in `.env` next to the compose file, so
changing either is a file edit plus `docker compose up -d` — no rebuild.

DSM already uses ports `5000`/`5001` and Container Manager occupies a range in the
`9xxx`s. `8787` is clear of both.

### Data volume

```yaml
volumes:
  - ${DATA_DIR:-./data}:/data
environment:
  - UNIFI_DB_PATH=/data/unifi_clients.db
```

On the NAS, `DATA_DIR=/volume1/docker/unifi-dashboard/data`, putting the database at
`/volume1/docker/unifi-dashboard/data/unifi_clients.db` — visible in File Station,
backed up by Hyper Backup, and surviving every image rebuild.

Three constraints that are easy to get wrong:

1. **Mount the directory, not the file.** `db.connect()` sets
   `PRAGMA journal_mode=WAL` (`unifi_lib/db.py:14`), which creates
   `unifi_clients.db-wal` and `unifi_clients.db-shm` sidecars alongside the database.
   A single-file bind mount breaks them.
2. **The volume must be local storage, not an NFS/SMB share.** SQLite WAL depends on
   shared-memory locking that network filesystems do not implement correctly, and
   will corrupt the database. `/volume1/...` on the NAS's own btrfs/ext4 is correct.
3. **Ownership must match.** The container runs as uid 1000, so the data directory
   needs a one-time `chown` on the NAS. Getting this wrong produces a permission
   error at first write that reads like an application bug.

### Files added

| File | Role |
| --- | --- |
| `Dockerfile` | `python:3.14-slim`, pinned deps, non-root user, healthcheck |
| `requirements.txt` | the three pinned dependencies above |
| `docker-compose.yml` | volume, ports, env, `restart: unless-stopped`, log rotation |
| `.env.example` | documents every variable; the real `.env` is gitignored |
| `.dockerignore` | excludes the database and log from the build context |
| `.gitignore` | same exclusions — load-bearing, see Section 3 |
| `.github/workflows/docker-publish.yml` | multi-arch build and push to ghcr.io |

### Healthcheck

Hits `/` (which serves the static HTML via `handle_index`, `live_server.py:363` — no
controller round-trip, so it stays cheap) using Python's `urllib`. `python:3.14-slim`
ships neither `curl` nor `wget`, and adding a package purely for the healthcheck is
not worth the image size.

### Log rotation

The compose log driver is configured with `max-size: 10m`, `max-file: 3`. Because
`live_server.py` already logs to stdout, this permanently resolves the unbounded
`live_server.log` growth noted in CLAUDE.md.

## Section 3 — CI to ghcr.io to NAS

```text
git push ──► GitHub Actions ──► buildx (amd64 + arm64) ──► ghcr.io/<owner>/unifi-dashboard
                                                                     │
                                           NAS: docker compose pull ─┘ && up -d
```

### Multi-architecture

The workflow builds **both** `linux/amd64` and `linux/arm64`. Most Synology models
are x86, but the entry-level `j` and `play` lines are ARM. Building both costs CI
minutes only, removes the need to determine the NAS architecture in advance, and
keeps the image portable if the target host changes later.

### Registry authentication

A private repository produces a private ghcr package, so:

- **Pushing from Actions** needs no PAT — the built-in `GITHUB_TOKEN` with
  `packages: write` is sufficient.
- **Pulling on the NAS** requires Container Manager → Registry → add `ghcr.io` with
  the GitHub username and a classic PAT scoped to `read:packages`.

The workflow references `${{ github.repository_owner }}` and compose reads `IMAGE`
from `.env`, so the GitHub username is never hardcoded in the repository.

### The PII hazard — ordering is mandatory

This project is not currently a git repository. Three things must never be committed:

- `unifi_clients.db` (plus its `-wal`/`-shm` sidecars) — 222 MB of client MACs,
  hostnames, and IPs
- `live_server.log` — 12 MB of the same data in text form
- `.env` — holds the live controller username and password on the NAS

`__pycache__/` should be excluded too, for tidiness rather than safety.

If either is committed it is in git history permanently; GitHub keeps unreferenced
objects reachable even after a force-push, and MAC addresses cannot be rotated the
way a leaked credential can.

Therefore the required order is:

1. Write `.gitignore` **first**
2. `git init`
3. Inspect `git status` and confirm the staged list contains no `.db` or `.log`
4. Only then, the first commit

The same exclusions go in `.dockerignore` for a different reason: without it every
build ships ~234 MB of context to the daemon and risks baking client history into an
image layer.

The repository must be **private**.

## Section 4 — Exposure

**Decision: the dashboard is published to the LAN with no access restriction.**

This is a deliberate, accepted choice, recorded here so it is not mistaken later for
an oversight.

The dashboard has no authentication of any kind — no login, no token, no session.
Today it is safe by accident: bound to `127.0.0.1` on the Mac, only that machine's
user can reach it. Publishing port `8787` on the NAS removes that property. Any
device on the LAN — including IoT devices, anything on a guest VLAN able to route to
the NAS, and any compromised machine — can load the full dashboard, which exposes the
client inventory, firewall policies, VPN configuration, traffic flows, and WiFi
topology.

If this is revisited, note that a DSM → Security → Firewall rule restricting port
`8787` may not actually take effect: Docker publishes ports via DNAT rules that
commonly bypass the DSM firewall's INPUT chain, so the container can remain reachable
regardless of the DSM rule. The dependable alternatives are binding the published
port to the NAS loopback in `docker-compose.yml` (e.g.
`127.0.0.1:${HOST_PORT}:${BIND_PORT}`) and fronting it with something authenticated,
or using a VPN/Tailscale overlay instead of a LAN-facing port at all.

## Section 5 — Verification and rollback

Docker Desktop is now running locally (Docker 29.6.2 on `linux/aarch64`, Compose
v5.3.1, buildx v0.35.0), so the container is fully testable before anything reaches
GitHub or the NAS. The NAS becomes a deployment of a known-good artifact rather than
the first place the container has ever run.

### Stage A — local

1. **Backward-compatibility regression.** After the Section 1 edits, restart the
   existing launchd job with no env vars set and confirm the Mac dashboard behaves
   identically. This proves the defaults preserve current behaviour.
2. **`docker build`** — proves the Dockerfile is valid and that the three pinned
   dependencies actually resolve on `python:3.14-slim`.
3. **Prove `.dockerignore` works.** The highest-value check, because it guards the
   PII risk:
   - build context reports KB, not 234 MB
   - `docker run --rm IMAGE ls -la /app` shows no `.db` and no `.log`
   - `docker history --no-trunc IMAGE` shows no oversized layer
4. **Run against the live controller** on port `8788` with a throwaway data
   directory, alongside the still-running launchd instance. Watch
   `docker compose logs -f` through one full `persist_loop` **and** one `slow_loop`
   for exceptions, then load `localhost:8788` and confirm WebSocket ticks and a
   rendered chart.
5. **Volume and WAL check.** Confirm `unifi_clients.db`, `-wal`, and `-shm` all
   appear in the host directory, then `docker compose restart` and confirm the data
   survived.
6. **Permission failure drill.** Chown the data directory to the wrong uid, confirm
   the failure is a clear error rather than silent corruption, then correct it and
   confirm success. Bind-mount ownership is the most common Synology deploy failure.
7. **Healthcheck** — `docker inspect --format '{{.State.Health.Status}}'` reports
   `healthy`.
8. **Cross-build `linux/amd64`** via buildx on this arm64 Mac and smoke-run it under
   emulation, proving the NAS-architecture image builds and executes.
9. **Secret scan** — confirm no credentials are baked into the image
   (`docker run --rm IMAGE env`, and confirm no `settings.local.json` is present).

### Stage B — CI

Confirms Actions reproduces a build that already demonstrably works locally, and that
both architectures push successfully. Requires `gh auth login`, an interactive flow.

### Stage C — NAS

Only NAS-specific unknowns remain: ghcr registry authentication, the real CPU
architecture, DSM port conflicts, and volume ownership. Deploy, then repeat the
Stage A step 4 and 5 checks against the NAS instance.

### Stage D — retire launchd

`launchctl bootout` the `com.hector.unifi-live-monitor` job, and remove the plist
from `~/Library/LaunchAgents/`.

### Rollback

Free until Stage D. The Mac instance runs untouched throughout, so a failed container
costs only time. The two instances polling the same controller concurrently during
Stage A adds modest API load for a short window, which is acceptable.

## Out of scope

- Migrating the existing 222 MB database — explicitly decided against.
- Adding authentication to the dashboard — see Section 4.
- Any change to the polling architecture, schema, or frontend.
- Removing `poll_unifi.py`, which stays as the legacy manual-backfill tool.

## Documentation follow-up

CLAUDE.md's **Running** and **Credentials** sections describe launchd and the
`settings.local.json` path as the only mechanisms. Both must be updated once Stage D
completes, along with the **Database growth** note that `live_server.log` grows
unbounded — the compose log driver resolves that.
