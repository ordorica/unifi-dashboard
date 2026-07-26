# Docker Containerization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the UniFi live dashboard off this Mac's launchd job into a Docker container running on a Synology/QNAP NAS, built by GitHub Actions and pulled from a private ghcr.io registry.

**Architecture:** Three hardcoded values in the existing Python become environment-overridable, each keeping its current value as the default so the Mac's behaviour is untouched. A `python:3.14-slim` image runs the same code as a non-root user with the SQLite database on a bind-mounted volume. GitHub Actions builds `linux/amd64` + `linux/arm64` and pushes to ghcr.io; the NAS only ever pulls.

**Tech Stack:** Python 3.14, aiohttp, SQLite (WAL), Docker + Compose v5, buildx, GitHub Actions, ghcr.io, Synology Container Manager.

**Spec:** `docs/superpowers/specs/2026-07-26-docker-containerization-design.md`

## Global Constraints

- Dependencies pinned exactly: `unifi-core==0.4.20`, `aiounifi==92`, `aiohttp==3.14.3`. These were read from the live `uv` environment — do not bump them in this plan.
- Base image: `python:3.14-slim`. Publishes both `linux/amd64` and `linux/arm64`.
- Container runs as **uid/gid 1000**, non-root.
- Every env var keeps the current hardcoded value as its default. Behaviour with no env set must be byte-identical to today.
- **Never commit** `*.db`, `*.db-wal`, `*.db-shm`, `*.log`, `.env`. These hold live client MACs, hostnames, IPs, and controller credentials. Already enforced by `.gitignore` as of commit `8716c49`.
- The GitHub repository must be **private**.
- All timestamps in this codebase are UTC. Do not add `TZ` configuration.
- The launchd job `com.hector.unifi-live-monitor` keeps running until Task 6. It is the rollback path.
- This project has **no test framework**. Verification is executable shell commands, matching the codebase's existing idiom (see CLAUDE.md "Running"). Do not introduce pytest.
- Run all commands from `/Users/hector/Projects/.unifi-dashboard`.

---

### Task 1: Make bind address, database path, and credentials environment-configurable

**Files:**
- Modify: `live_server.py` (add `import os`; line 37)
- Modify: `unifi_lib/db.py` (add `import os`; line 8)
- Modify: `unifi_lib/fetch.py` (add `import os`; lines 19–33)

**Interfaces:**
- Consumes: nothing.
- Produces: env vars `BIND_HOST` (default `127.0.0.1`), `BIND_PORT` (default `8787`), `UNIFI_DB_PATH` (default `<repo>/unifi_clients.db`), and credential vars `UNIFI_NETWORK_HOST` / `UNIFI_HOST`, `UNIFI_NETWORK_USERNAME` / `UNIFI_USERNAME`, `UNIFI_NETWORK_PASSWORD` / `UNIFI_PASSWORD`, `UNIFI_NETWORK_PORT`, `UNIFI_NETWORK_SITE`, `UNIFI_NETWORK_VERIFY_SSL`. `load_config()` keeps its existing return shape: `{"host", "username", "password", "port", "site", "verify_ssl"}`. Tasks 2 and 3 depend on all of these names exactly.

None of these three files currently import `os` — verified. Each needs the import added.

- [ ] **Step 1: Write the failing verification script**

Create `scratch_verify_env.sh` (temporary, deleted in Step 8):

```bash
#!/usr/bin/env bash
# Verification for Task 1. Not committed.
#
# Each check sets its environment *inside* Python, before the import that
# reads it. DB_FILE, HOST and PORT are all evaluated at import time, so this
# works -- and it avoids `VAR=x shell_function`, whose persistence semantics
# differ between bash modes and would let env bleed between checks.
set -u
RUN="uv run --python 3.14 --with unifi-core --with aiounifi python3"
pass=0; fail=0
check() {
  if out=$($RUN -c "$2" 2>&1); then echo "PASS  $1"; pass=$((pass+1))
  else echo "FAIL  $1"; echo "$out" | tail -3 | sed 's/^/      /'; fail=$((fail+1)); fi
}

check "db: default path unchanged" '
from unifi_lib import db
assert db.DB_FILE.name == "unifi_clients.db", db.DB_FILE
assert db.DB_FILE.parent.name == ".unifi-dashboard", db.DB_FILE'

check "db: UNIFI_DB_PATH honored" '
import os; os.environ["UNIFI_DB_PATH"] = "/tmp/t.db"
from unifi_lib import db
assert str(db.DB_FILE) == "/tmp/t.db", db.DB_FILE'

check "server: default bind unchanged" '
import live_server as s
assert (s.HOST, s.PORT) == ("127.0.0.1", 8787), (s.HOST, s.PORT)'

check "server: bind overridable" '
import os; os.environ["BIND_HOST"] = "0.0.0.0"; os.environ["BIND_PORT"] = "9999"
import live_server as s
assert (s.HOST, s.PORT) == ("0.0.0.0", 9999), (s.HOST, s.PORT)
assert isinstance(s.PORT, int), type(s.PORT)'

check "fetch: settings.local.json still used" '
from unifi_lib.fetch import load_config
c = load_config()
assert c["host"] and c["username"] and c["password"], c'

check "fetch: env wins over settings file" '
import os
os.environ.update(UNIFI_NETWORK_HOST="1.2.3.4", UNIFI_NETWORK_USERNAME="u",
                  UNIFI_NETWORK_PASSWORD="p")
from unifi_lib.fetch import load_config
c = load_config()
assert c["host"] == "1.2.3.4", c["host"]
assert c["username"] == "u" and c["password"] == "p", c'

check "fetch: absent settings file tolerated" '
import os
os.environ.update(UNIFI_NETWORK_HOST="1.2.3.4", UNIFI_NETWORK_USERNAME="u",
                  UNIFI_NETWORK_PASSWORD="p")
from pathlib import Path
import unifi_lib.fetch as f
f.SETTINGS_FILE = Path("/nonexistent/settings.local.json")
c = f.load_config()
assert c["host"] == "1.2.3.4", c
assert c["port"] == 443 and c["site"] == "default", c'

check "fetch: still fails loudly when nothing is set" '
from pathlib import Path
import unifi_lib.fetch as f
f.SETTINGS_FILE = Path("/nonexistent/settings.local.json")
try:
    f.UnifiSession()
    raise SystemExit("expected RuntimeError, got a session")
except RuntimeError:
    pass'

echo "---"; echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
```

Note the last check: a container with no credentials must crash at startup rather than silently connecting to nothing.

- [ ] **Step 2: Run it to confirm the new behaviours fail**

```bash
chmod +x scratch_verify_env.sh && ./scratch_verify_env.sh
```

Expected: the three "default/unchanged" checks PASS (nothing has changed yet); "UNIFI_DB_PATH honored", "bind overridable", "env wins", and "absent settings file tolerated" FAIL. If "still fails loudly" fails, stop — that means the current `RuntimeError` guard is already broken.

- [ ] **Step 3: Make the bind address configurable**

In `live_server.py`, add `import os` to the stdlib import block (alphabetically, between `import logging` and `import re`), then replace line 37:

```python
# Loopback default preserves the macOS/launchd behaviour; the container
# overrides BIND_HOST to 0.0.0.0, since 127.0.0.1 inside a container is
# unreachable from the host.
HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BIND_PORT", "8787"))
```

- [ ] **Step 4: Make the database path configurable**

In `unifi_lib/db.py`, add `import os` above `import sqlite3`, then replace line 8:

```python
# Container deployments point this at a mounted volume so the database
# survives image rebuilds. Default keeps the file beside the code, as before.
DB_FILE = Path(os.environ.get("UNIFI_DB_PATH")
               or Path(__file__).resolve().parent.parent / "unifi_clients.db")
```

- [ ] **Step 5: Make credentials readable from the environment**

In `unifi_lib/fetch.py`, add `import os` above `import json`, then replace `load_config()` (lines 22–33) with:

```python
def _settings_env() -> dict:
    """The `env` block of settings.local.json, or {} when it is absent.

    That file lives outside this directory and only exists on the development
    Mac. In a container every value arrives through the real environment, so a
    missing file is normal rather than an error.
    """
    try:
        return json.loads(SETTINGS_FILE.read_text()).get("env", {})
    except (OSError, ValueError):
        return {}


def load_config() -> dict:
    settings = _settings_env()

    def pick(*names, default=None):
        """Real environment first, then settings.local.json, then default."""
        for source in (os.environ, settings):
            for name in names:
                if source.get(name):
                    return source[name]
        return default

    return {
        "host": pick("UNIFI_NETWORK_HOST", "UNIFI_HOST"),
        "username": pick("UNIFI_NETWORK_USERNAME", "UNIFI_USERNAME"),
        "password": pick("UNIFI_NETWORK_PASSWORD", "UNIFI_PASSWORD"),
        "port": int(pick("UNIFI_NETWORK_PORT", "UNIFI_PORT", default=443)),
        "site": pick("UNIFI_NETWORK_SITE", "UNIFI_SITE", default="default"),
        "verify_ssl": str(pick("UNIFI_NETWORK_VERIFY_SSL", "UNIFI_VERIFY_SSL",
                               default="false")).lower() == "true",
    }
```

`UnifiSession.__init__` already raises `RuntimeError` when host/username/password are falsy — leave that untouched, it is the loud-failure guard.

- [ ] **Step 6: Run the verification script again**

```bash
./scratch_verify_env.sh
```

Expected: `8 passed, 0 failed`.

- [ ] **Step 7: Regression-test the live launchd instance**

This is the step that protects the running system. Syntax-check, restart, confirm unchanged:

```bash
for f in live_server.py unifi_lib/db.py unifi_lib/fetch.py; do
  python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$f" && echo "OK $f"
done
launchctl kickstart -k gui/$(id -u)/com.hector.unifi-live-monitor
sleep 20
tail -200 live_server.log | sed -n '/Live server ready/,$p' \
  | grep -i "loop failed\|tick failed\|Traceback" && echo "ERRORS FOUND" || echo "clean"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8787/
lsof -nP -iTCP:8787 -sTCP:LISTEN | tail -1
```

Expected: three `OK` lines, `clean`, `HTTP 200`, and a listener still bound to **`127.0.0.1:8787`** — not `0.0.0.0`. If it bound to `0.0.0.0`, the default was not preserved; fix before continuing.

- [ ] **Step 8: Commit**

```bash
rm scratch_verify_env.sh
git add live_server.py unifi_lib/db.py unifi_lib/fetch.py
git commit -m "feat: make bind address, DB path and credentials env-configurable

Containerization prerequisite. Each value keeps its current hardcoded
value as the default, so the launchd instance is unaffected: BIND_HOST
stays 127.0.0.1, BIND_PORT 8787, and the database stays beside the code.

load_config() now prefers the real process environment over
settings.local.json and tolerates that file being absent, since it lives
outside the repo and will not exist in a container. UnifiSession still
raises RuntimeError when no credentials resolve from either source.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Build a clean container image

**Files:**
- Create: `requirements.txt`
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `BIND_HOST`, `BIND_PORT`, `UNIFI_DB_PATH` from Task 1.
- Produces: a local image tagged `unifi-dashboard:local`, listening on `0.0.0.0:8787`, database at `/data/unifi_clients.db`, running as uid 1000. Task 3 runs this image; Task 4 builds the same Dockerfile in CI.

- [ ] **Step 1: Create `.dockerignore` first**

Written before the Dockerfile so the very first build cannot pick up the 222 MB database:

```text
# Live network data and credentials -- must never enter the build context.
*.db
*.db-wal
*.db-shm
*.log
.env
data/

# Not needed at runtime.
.git/
.github/
docs/
__pycache__/
*.py[cod]
.DS_Store
.gitignore
.dockerignore
Dockerfile
docker-compose.yml
```

- [ ] **Step 2: Create `requirements.txt`**

```text
unifi-core==0.4.20
aiounifi==92
aiohttp==3.14.3
```

- [ ] **Step 3: Create the `Dockerfile`**

```dockerfile
FROM python:3.14-slim

# Fixed uid/gid so the NAS bind-mount chown is predictable: `chown -R 1000:1000`
# on the host data directory is all that is required.
RUN groupadd -g 1000 app && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin app

WORKDIR /app

# Dependencies before source, so code edits do not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY live_server.py poll_unifi.py ./
COPY unifi_lib/ ./unifi_lib/
COPY static/ ./static/

# The database lives on a mounted volume, never in the image.
RUN mkdir -p /data && chown 1000:1000 /data
VOLUME ["/data"]

ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8787 \
    UNIFI_DB_PATH=/data/unifi_clients.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8787

# `/` serves the static HTML with no controller round-trip, so this stays cheap.
# python:3.14-slim ships neither curl nor wget; urllib avoids adding a package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('BIND_PORT','8787') + '/', timeout=4).status == 200 else 1)"]

CMD ["python", "live_server.py"]
```

`PYTHONUNBUFFERED=1` matters: without it `docker logs` lags behind, because `live_server.py:48` logs to stdout.

- [ ] **Step 4: Build, and check the context size**

```bash
docker build -t unifi-dashboard:local . 2>&1 | tee /tmp/build.log
grep -i "transferring context" /tmp/build.log
```

Expected: build succeeds, and the build context reports **kilobytes, not ~234 MB**. A context in the hundreds of MB means `.dockerignore` is not being applied — stop and fix.

- [ ] **Step 5: Prove no client data or credentials are in the image**

```bash
echo "--- /app contents ---"
docker run --rm --entrypoint sh unifi-dashboard:local -c 'ls -la /app /app/unifi_lib'
echo "--- any db/log/env anywhere in the image? ---"
docker run --rm --entrypoint sh unifi-dashboard:local -c \
  'find / -xdev \( -name "*.db" -o -name "*.log" -o -name "settings.local.json" -o -name ".env" \) 2>/dev/null | head'
echo "--- layer sizes ---"
docker history unifi-dashboard:local --format '{{.Size}}\t{{.CreatedBy}}' | head -15
echo "--- baked-in secrets? ---"
docker run --rm unifi-dashboard:local env | grep -iE 'pass|secret|token|key' || echo "none"
```

Expected: `/app` holds only `live_server.py`, `poll_unifi.py`, `unifi_lib/`, `static/`. The `find` returns nothing. No layer is unexpectedly large. No credential-shaped env vars.

- [ ] **Step 6: Confirm it runs as non-root**

```bash
docker run --rm --entrypoint sh unifi-dashboard:local -c 'id'
```

Expected: `uid=1000(app) gid=1000(app)`.

- [ ] **Step 7: Commit**

```bash
git add .dockerignore requirements.txt Dockerfile
git commit -m "feat: add Dockerfile, pinned requirements and .dockerignore

python:3.14-slim running as uid 1000, database on a /data volume, deps
pinned to the versions the launchd instance is running today.

.dockerignore is written first and excludes unifi_clients.db and the
logs, keeping live client MACs, hostnames and IPs out of both the build
context and the image layers. Verified by inspecting the built image.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Run the container locally against the live controller

**Files:**
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create (untracked): `.env`, `data/`

**Interfaces:**
- Consumes: the `unifi-dashboard:local` image from Task 2.
- Produces: `.env` variables `IMAGE`, `HOST_PORT`, `BIND_PORT`, `DATA_DIR`, and the six `UNIFI_NETWORK_*` values. Tasks 4 and 5 reuse this compose file unchanged.

Runs on port **8788** so the launchd instance on 8787 keeps serving throughout.

- [ ] **Step 1: Create `docker-compose.yml`**

```yaml
services:
  unifi-dashboard:
    image: ${IMAGE:-unifi-dashboard:local}
    build: .
    container_name: unifi-dashboard
    restart: unless-stopped
    ports:
      # HOST_PORT is what you browse to; BIND_PORT is what the server listens
      # on inside the container. Normally only HOST_PORT changes.
      - "${HOST_PORT:-8787}:${BIND_PORT:-8787}"
    environment:
      BIND_HOST: 0.0.0.0
      BIND_PORT: ${BIND_PORT:-8787}
      UNIFI_DB_PATH: /data/unifi_clients.db
      UNIFI_NETWORK_HOST: ${UNIFI_NETWORK_HOST}
      UNIFI_NETWORK_USERNAME: ${UNIFI_NETWORK_USERNAME}
      UNIFI_NETWORK_PASSWORD: ${UNIFI_NETWORK_PASSWORD}
      UNIFI_NETWORK_PORT: ${UNIFI_NETWORK_PORT:-443}
      UNIFI_NETWORK_SITE: ${UNIFI_NETWORK_SITE:-default}
      UNIFI_NETWORK_VERIFY_SSL: ${UNIFI_NETWORK_VERIFY_SSL:-false}
    volumes:
      # Must be a directory, not a file: WAL mode creates -wal and -shm
      # sidecars beside the database. Must be local storage, never NFS/SMB.
      - ${DATA_DIR:-./data}:/data
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 2: Create `.env.example`**

```text
# Copy to .env and fill in. .env is gitignored: it holds live credentials.

# --- Image ---
# Local build:  unifi-dashboard:local
# NAS (pulled): ghcr.io/<your-github-username>/unifi-dashboard:latest
IMAGE=unifi-dashboard:local

# --- Ports ---
# HOST_PORT is what you browse to. BIND_PORT is the in-container listen port
# and rarely needs changing. DSM occupies 5000/5001 and a range in the 9xxx's.
HOST_PORT=8787
BIND_PORT=8787

# --- Data ---
# Directory holding unifi_clients.db plus its -wal/-shm sidecars.
# Must be local storage (not NFS/SMB) and owned by uid 1000.
# NAS example: /volume1/docker/unifi-dashboard/data
DATA_DIR=./data

# --- UniFi controller ---
UNIFI_NETWORK_HOST=
UNIFI_NETWORK_USERNAME=
UNIFI_NETWORK_PASSWORD=
UNIFI_NETWORK_PORT=443
UNIFI_NETWORK_SITE=default
UNIFI_NETWORK_VERIFY_SSL=false
```

- [ ] **Step 3: Create a local `.env` on port 8788**

Credentials are copied from the existing settings file rather than retyped:

```bash
cp .env.example .env
python3 - <<'EOF'
import json, pathlib
env = json.loads(pathlib.Path("/Users/hector/Projects/.claude/settings.local.json").read_text())["env"]
p = pathlib.Path(".env"); t = p.read_text()
t = t.replace("HOST_PORT=8787", "HOST_PORT=8788")
for k in ("UNIFI_NETWORK_HOST", "UNIFI_NETWORK_USERNAME", "UNIFI_NETWORK_PASSWORD"):
    t = t.replace(f"{k}=\n", f"{k}={env[k]}\n")
p.write_text(t)
EOF
mkdir -p data
git check-ignore -v .env data && echo "OK: both ignored by git"
```

Expected: `git check-ignore` confirms both are ignored. If it does not, **stop** — `.env` holds your controller password.

- [ ] **Step 4: Start it and watch a full cycle**

```bash
docker compose up -d
sleep 90   # one persist_loop (60s) plus margin
docker compose logs --no-color | grep -iE "Live server ready|loop failed|tick failed|Traceback" | head -20
```

Expected: `Live server ready`, and **no** `loop failed` / `tick failed` / `Traceback`. `persist_loop` and `slow_loop` both run at 60s, so 90s covers both.

- [ ] **Step 5: Confirm it serves and the WebSocket ticks**

```bash
curl -sS -o /dev/null -w 'index: HTTP %{http_code}\n' http://127.0.0.1:8788/
curl -sS -o /dev/null -w 'api:   HTTP %{http_code}\n' http://127.0.0.1:8788/api/networks
```

Then open <http://127.0.0.1:8788> in a browser and confirm the Overview tab shows live numbers changing each second and a chart renders. The per-second updates arriving is the WebSocket working.

- [ ] **Step 6: Confirm the volume and WAL sidecars are real**

```bash
ls -la data/
docker compose exec unifi-dashboard sh -c 'ls -la /data && id'
sqlite3 data/unifi_clients.db "select name from sqlite_master where type='table' order by 1;" | head
sqlite3 data/unifi_clients.db "select count(*) from clients;"
```

Expected: `unifi_clients.db` **plus `unifi_clients.db-wal` and `-shm`** on the host, owned by uid 1000; tables present; a non-zero client count. If the `-wal` file is missing, the mount is wrong.

- [ ] **Step 7: Confirm data survives a restart**

```bash
before=$(sqlite3 data/unifi_clients.db "select count(*) from clients;")
docker compose restart && sleep 20
after=$(sqlite3 data/unifi_clients.db "select count(*) from clients;")
echo "before=$before after=$after"
[ "$after" -ge "$before" ] && echo "PASS: data persisted" || echo "FAIL: data lost"
```

- [ ] **Step 8: Confirm the healthcheck reports healthy**

```bash
sleep 40   # start-period is 30s
docker inspect --format '{{.State.Health.Status}}' unifi-dashboard
```

Expected: `healthy`.

- [ ] **Step 9: Permission failure drill**

The most common Synology deployment failure, rehearsed here where it is cheap:

```bash
docker compose down
sudo chown -R 65534:65534 data/          # deliberately wrong owner
docker compose up -d && sleep 15
docker compose logs --tail 30 | grep -iE "permission|readonly|unable to open" | head -5
```

Expected: a clear permission error mentioning the database — **not** silent success and not corruption. Record the exact message; it is what you will see on the NAS if the `chown` is missed. Then restore:

```bash
docker compose down
sudo chown -R "$(id -u):$(id -g)" data/
docker compose up -d && sleep 20
docker inspect --format '{{.State.Health.Status}}' unifi-dashboard
```

Expected: `healthy` again.

- [ ] **Step 10: Stop the local container and commit**

```bash
docker compose down
git status --porcelain          # .env and data/ must NOT appear
git add docker-compose.yml .env.example
git commit -m "feat: add compose file and env template

Publishes HOST_PORT to BIND_PORT so the browse port can change without a
rebuild, bind-mounts DATA_DIR to /data for the SQLite database, and caps
container logs at 3x10MB -- which resolves the unbounded live_server.log
growth, since the app already logs to stdout.

Verified locally against the live controller on port 8788 alongside the
running launchd instance: full persist_loop and slow_loop cycle with no
errors, WAL sidecars present on the host mount, data surviving a restart,
healthcheck green, and a deliberate wrong-owner drill producing a clear
permission error rather than corruption.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Publish multi-architecture images from GitHub Actions

**Files:**
- Create: `.github/workflows/docker-publish.yml`

**Interfaces:**
- Consumes: the `Dockerfile` from Task 2.
- Produces: `ghcr.io/<owner>/unifi-dashboard:latest` and `:<sha>` for `linux/amd64` + `linux/arm64`. Task 5 pulls these.

- [ ] **Step 1: Prove the NAS architecture builds locally first**

Do not discover an `exec format error` on the NAS. This Mac is arm64, so amd64 is the risky one:

```bash
docker buildx build --platform linux/amd64 -t unifi-dashboard:amd64 --load .
docker run --rm --platform linux/amd64 --entrypoint sh unifi-dashboard:amd64 \
  -c 'python -c "import aiohttp, aiounifi, unifi_core; print(\"imports OK\")" && id'
```

Expected: build succeeds, `imports OK`, `uid=1000(app)`. This runs under emulation and is slow — that is expected and not a problem, it only proves the image executes.

- [ ] **Step 2: Create the workflow**

```yaml
name: Build and publish image

on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - uses: docker/setup-qemu-action@v3

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # repository_owner keeps the username out of the committed files.
      - id: meta
        run: echo "repo=ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/unifi-dashboard" >> "$GITHUB_OUTPUT"

      - uses: docker/build-push-action@v6
        with:
          context: .
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ${{ steps.meta.outputs.repo }}:latest
            ${{ steps.meta.outputs.repo }}:${{ github.sha }}
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

`${GITHUB_REPOSITORY_OWNER,,}` lowercases the owner — ghcr rejects uppercase in image paths.

- [ ] **Step 3: Authenticate `gh` (interactive — requires you)**

```bash
gh auth login
gh auth status
```

- [ ] **Step 4: Create the private repository and push**

```bash
git add .github/workflows/docker-publish.yml
git commit -m "ci: build and publish multi-arch image to ghcr.io

Builds linux/amd64 and linux/arm64 so the image runs on both x86 and ARM
Synology models without knowing the target in advance. Pushes with the
built-in GITHUB_TOKEN; no PAT is needed for publishing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"

gh repo create unifi-dashboard --private --source=. --remote=origin --push
```

- [ ] **Step 5: Verify the published manifest covers both architectures**

```bash
gh run watch
docker buildx imagetools inspect "ghcr.io/$(gh api user --jq .login | tr 'A-Z' 'a-z')/unifi-dashboard:latest"
```

Expected: the workflow succeeds and the manifest lists **both** `linux/amd64` and `linux/arm64`.

- [ ] **Step 6: Confirm the repository leaked nothing**

```bash
gh repo view --json visibility --jq .visibility
git ls-files | grep -iE '\.(db|log|env)$' && echo "LEAK" || echo "clean: no db/log/env tracked"
```

Expected: `PRIVATE`, and `clean`.

---

### Task 5: Deploy to the NAS

**Files:** none in the repo. This task runs on the NAS.

**Interfaces:**
- Consumes: the published image from Task 4 and `docker-compose.yml` / `.env.example` from Task 3.
- Produces: a running container on the NAS. Task 6 depends on this being verified.

- [ ] **Step 1: Create the data directory with the right owner**

Over SSH to the NAS:

```bash
sudo mkdir -p /volume1/docker/unifi-dashboard/data
sudo chown -R 1000:1000 /volume1/docker/unifi-dashboard/data
uname -m    # record: x86_64 or aarch64
```

`/volume1` must be the NAS's own storage. Do not use a mounted NFS/SMB share — SQLite WAL will corrupt on one.

- [ ] **Step 2: Authenticate to ghcr.io**

Create a GitHub classic PAT with only `read:packages`, then either use Container Manager → Registry → Settings → Add (`https://ghcr.io`, your username, the PAT), or over SSH:

```bash
echo "<PAT>" | docker login ghcr.io -u "<github-username>" --password-stdin
```

- [ ] **Step 3: Copy the compose file and write the NAS `.env`**

From the Mac:

```bash
scp docker-compose.yml .env.example <nas>:/volume1/docker/unifi-dashboard/
```

On the NAS, `cp .env.example .env` and set:

```text
IMAGE=ghcr.io/<your-github-username>/unifi-dashboard:latest
HOST_PORT=8787
BIND_PORT=8787
DATA_DIR=/volume1/docker/unifi-dashboard/data
UNIFI_NETWORK_HOST=<controller ip>
UNIFI_NETWORK_USERNAME=<username>
UNIFI_NETWORK_PASSWORD=<password>
```

- [ ] **Step 4: Pull and start**

```bash
cd /volume1/docker/unifi-dashboard
docker compose pull
docker compose up -d
sleep 90
docker compose logs --no-color | grep -iE "Live server ready|loop failed|tick failed|Traceback" | head -20
```

Expected: `Live server ready`, no errors. Pull — not build; the NAS has no source.

- [ ] **Step 5: Repeat the Task 3 checks against the NAS**

```bash
ls -la /volume1/docker/unifi-dashboard/data/
docker inspect --format '{{.State.Health.Status}}' unifi-dashboard
```

Expected: `unifi_clients.db` plus `-wal` and `-shm`, and `healthy`.

Then from a browser on the LAN, open `http://<nas-ip>:8787` and confirm live per-second numbers and a rendered chart.

- [ ] **Step 6: Confirm it survives a reboot**

`restart: unless-stopped` is only a claim until tested:

```bash
sudo reboot
# wait, then:
docker ps --filter name=unifi-dashboard --format '{{.Status}}'
```

Expected: running, healthy.

---

### Task 6: Retire launchd and update the documentation

**Files:**
- Modify: `CLAUDE.md` (What this is; Running; Credentials; Database growth)
- Delete: `~/Library/LaunchAgents/com.hector.unifi-live-monitor.plist`

**Interfaces:**
- Consumes: a verified NAS deployment from Task 5.

**Do not start this task until Task 5 is fully verified.** The launchd job is the rollback path.

- [ ] **Step 1: Stop and remove the launchd job**

```bash
launchctl bootout gui/$(id -u)/com.hector.unifi-live-monitor
rm ~/Library/LaunchAgents/com.hector.unifi-live-monitor.plist
launchctl list | grep unifi && echo "STILL LOADED" || echo "unloaded"
lsof -nP -iTCP:8787 -sTCP:LISTEN || echo "port 8787 free on the Mac"
```

- [ ] **Step 2: Update CLAUDE.md "What this is"**

Replace the parenthetical about loopback-only binding, which is no longer true:

```markdown
on `http://<nas-ip>:8787`. It runs as a Docker container on the NAS; the
dashboard has no authentication and is published to the LAN unrestricted,
which is a deliberate choice recorded in
`docs/superpowers/specs/2026-07-26-docker-containerization-design.md`.
```

- [ ] **Step 3: Rewrite CLAUDE.md "Running"**

Four-backtick fence, because the replacement text itself contains fenced blocks:

````markdown
## Running

The server runs as a Docker container on the NAS, defined by
`docker-compose.yml`. Images are built by GitHub Actions for linux/amd64 and
linux/arm64 and published to ghcr.io; the NAS only pulls.

Deploy a change: push to `main`, wait for the Actions run, then on the NAS:

```bash
cd /volume1/docker/unifi-dashboard && docker compose pull && docker compose up -d
```

Run locally against the live controller (use a spare port to avoid clashing
with anything else):

```bash
cp .env.example .env    # fill in credentials; .env is gitignored
HOST_PORT=8788 docker compose up -d --build
docker compose logs -f
```

Run in the foreground without Docker, as before:

```bash
uv run --python 3.14 --with unifi-core --with aiounifi python3 live_server.py
```

Syntax-check before deploying (there is no linter configured):

```bash
python3 -c "import ast; ast.parse(open('live_server.py').read())"
```

Check for errors after a deploy:

```bash
docker compose logs --no-color | grep -iE "loop failed|tick failed|Traceback"
```

Configuration is entirely environment variables — see `.env.example`.
`BIND_HOST`, `BIND_PORT` and `UNIFI_DB_PATH` all default to the original
hardcoded values, so running the code directly still behaves as it always did.

`poll_unifi.py` is a legacy one-shot poller kept for manual backfills; the live
server owns all recurring polling now.
````

- [ ] **Step 4: Rewrite CLAUDE.md "Credentials"**

```markdown
## Credentials

`unifi_lib/fetch.py` reads the controller host/username/password from the
process environment first (`UNIFI_NETWORK_HOST`, `UNIFI_NETWORK_USERNAME`,
`UNIFI_NETWORK_PASSWORD`), falling back to the `env` block of
`../../.claude/settings.local.json` when that file exists — which it does only
on the development Mac. In the container the values come from `.env`, which is
gitignored and must never be committed.

`unifi_clients.db` contains real client MACs, hostnames and IPs. It, the logs
and `.env` are all excluded by `.gitignore` and `.dockerignore`; that exclusion
is load-bearing, since this repository is on GitHub.
```

- [ ] **Step 5: Fix the stale log-growth note**

In "Database growth", replace the final sentence about `live_server.log` growing unbounded:

```markdown
Container logs are capped by the compose json-file driver at 3 × 10MB, so the
aiohttp access logging that previously grew `live_server.log` without limit is
now bounded.
```

- [ ] **Step 6: Commit and push**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for the containerized deployment

launchd is retired; the dashboard now runs as a container on the NAS.
Rewrites Running and Credentials, corrects the loopback-only claim in
What this is, and marks the unbounded-log note resolved by the compose
log driver.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
git push
```

---

## Rollback

Free through Task 5. The launchd instance runs untouched until Task 6 Step 1, so at any earlier point: `docker compose down` on the NAS and the Mac keeps serving. After Task 6, rolling back means restoring the plist from git history (`git show 8716c49`) and reloading it with `launchctl bootstrap`.

## Notes on verification

Two instances poll the same controller concurrently during Task 3 — the launchd one and the container. This roughly doubles controller API load for that window, which is acceptable and ends when Task 3 Step 10 stops the local container.

The NAS starts with an empty database by design. Long-range charts will be sparse until `backfill_gateway_history()` seeds `gateway_stats` from the controller's own rollups at startup (24h at 5-minute resolution, 7d hourly, 30d daily), and per-minute history accumulates from then on.
