# Kodosumi Scaling Enhancements

> Fork: [Bajuzjefe/kodosumi](https://github.com/Bajuzjefe/kodosumi)
> Upstream: [masumi-network/kodosumi](https://github.com/masumi-network/kodosumi)
> Status: Implemented — 4 PRs open for review

This fork adds **production scaling capabilities** to Kodosumi — PostgreSQL, Redis Streams, Temporal durable execution, and Docker Compose. All additions are **opt-in** and **backwards-compatible**. Zero changes to default behavior.

---

## Open Pull Requests

| PR | Branch | Title | Tests |
|----|--------|-------|-------|
| [#30](https://github.com/masumi-network/kodosumi/pull/30) | `feat/postgres-support` | PostgreSQL support alongside SQLite | 15 tests |
| [#31](https://github.com/masumi-network/kodosumi/pull/31) | `feat/redis-transport` | Redis Streams event transport | 28 tests |
| [#32](https://github.com/masumi-network/kodosumi/pull/32) | `feat/temporal-durability` | Temporal durable execution layer | 18 tests |
| [#33](https://github.com/masumi-network/kodosumi/pull/33) | `feat/docker-compose` | Docker Compose development environment | Manual |

---

## Why These Changes

Kodosumi v1.1.0 is a well-architected runtime for interactive AI agents. However, scaling beyond a single-node development setup reveals concrete bottlenecks:

| Problem | Impact | Solution (this fork) |
|---------|--------|----------------------|
| **SQLite is single-writer, file-bound** | Cannot share DB across cluster nodes, limits to ~1000 concurrent users | PR 1: PostgreSQL support |
| **Spooler polls Ray actors every 0.25s** | Adds latency under load, can't run multiple spoolers | PR 2: Redis Streams event transport |
| **No crash recovery for agent jobs** | If a paid job crashes mid-execution, escrowed payment is stuck | PR 3: Temporal durable execution |
| **No containerized deployment** | High onboarding friction — requires manual Ray cluster setup | PR 4: Docker Compose |

These issues are acknowledged by the upstream project (see issues [#8](https://github.com/masumi-network/kodosumi/issues/8) and [#11](https://github.com/masumi-network/kodosumi/issues/11)).

---

## What's Added

### PR 1: PostgreSQL Support ([#30](https://github.com/masumi-network/kodosumi/pull/30))

**Branch:** `feat/postgres-support`
**Config:** `KODO_EXECUTION_DATABASE=postgresql+asyncpg://user:pass@host:5432/db`

Adds PostgreSQL as an optional database backend for execution event storage.

**New/modified files:**

| File | Action | What |
|------|--------|------|
| `kodosumi/storage.py` | **New** | `SpoolerWriter` Protocol + `SqliteSpoolerWriter` + `PostgresSpoolerWriter` + `PostgresExecutionReader` + `create_spooler_writer()` factory |
| `kodosumi/dtypes.py` | Modified | Added `ExecutionEvent` SQLAlchemy model with indexed `fid`, `username`, `kind` columns |
| `kodosumi/config.py` | Modified | Added `EXECUTION_DATABASE: Optional[str] = None` setting |
| `kodosumi/spooler.py` | Modified | Refactored to use `SpoolerWriter` protocol; writer injected via `__init__` |
| `kodosumi/service/store.py` | Modified | Added `_connect_postgres()` path alongside existing `_connect_sqlite()` |
| `kodosumi/service/app.py` | Modified | Creates `PostgresExecutionReader` in app state when `EXECUTION_DATABASE` is set |
| `kodosumi/cli.py` | Modified | Added `koco db --create-tables` command for schema initialization |
| `pyproject.toml` | Modified | Added `postgres = ["asyncpg>=0.29.0", "psycopg[binary]>=3.1.0"]` optional dep |
| `tests/test_storage.py` | **New** | 15 unit tests covering writers, factory, and model |

**Key design decisions:**
- `SpoolerWriter` is a `runtime_checkable` Protocol (duck-typing, no ABCs)
- `PostgresSpoolerWriter` uses `psycopg` (sync driver) because the Spooler's save path is synchronous
- `PostgresExecutionReader` uses `asyncpg` via SQLAlchemy for async reads from the service layer
- URL conversion: accepts `postgresql+asyncpg://` URLs and converts to `postgresql://` for psycopg

**How it works:**
- When `KODO_EXECUTION_DATABASE` is not set (default): current per-user SQLite behavior, unchanged
- When set to a PostgreSQL URL: events are stored in a centralized `execution_events` table
- The admin DB (`KODO_ADMIN_DATABASE`) also supports PostgreSQL URLs
- Run `koco db --create-tables` to initialize the schema

**Dependencies:** `asyncpg` + `psycopg[binary]` (optional, via `pip install kodosumi[postgres]`)

---

### PR 2: Redis Streams Event Transport ([#31](https://github.com/masumi-network/kodosumi/pull/31))

**Branch:** `feat/redis-transport`
**Config:** `KODO_EVENT_TRANSPORT=redis`, `KODO_REDIS_URL=redis://localhost:6379`

Replaces the polling-based Ray queue event delivery with push-based Redis Streams using consumer groups.

**New/modified files:**

| File | Action | What |
|------|--------|------|
| `kodosumi/transport.py` | **New** | `EventProducer`/`EventConsumer` Protocols + `RayQueueProducer`/`Consumer` + `RedisStreamProducer`/`Consumer` + factories |
| `kodosumi/config.py` | Modified | Added `EVENT_TRANSPORT`, `REDIS_URL`, `REDIS_STREAM_PREFIX`, `REDIS_CONSUMER_GROUP`, `REDIS_BLOCK_MS`, `REDIS_MAX_STREAM_LEN` |
| `kodosumi/runner/tracer.py` | Modified | Uses `EventProducer` protocol instead of raw `ray.util.queue.Queue` |
| `kodosumi/runner/main.py` | Modified | Runner creates producer via factory; Launch() passes transport config |
| `kodosumi/spooler.py` | Modified | Added `_retrieve_redis()` path using `XREADGROUP` alongside `_retrieve_ray()` |
| `pyproject.toml` | Modified | Added `redis = ["redis[hiredis]>=5.0.0"]` optional dep |
| `tests/test_transport.py` | **New** | 28 unit tests with mocked Redis client |

**Key design decisions:**
- `RedisStreamProducer` is pickle-safe (lazy Redis client) for Ray actor serialization
- One Redis stream per execution (`kodo:events:{fid}`)
- Consumer groups (`XREADGROUP`) enable multiple spoolers
- Sync Redis client + `run_in_executor` for async contexts (avoids dual client complexity)
- `Tracer.__reduce__` updated for the new producer field

**How it works:**
- When `KODO_EVENT_TRANSPORT=ray` (default): current Ray queue polling, unchanged
- When set to `redis`: Tracer publishes via `XADD`, Spooler consumes via `XREADGROUP` with blocking reads
- Consumer groups enable **multiple spooler instances** to cooperatively process events
- `XACK` marks events as processed; unacknowledged events are redelivered

**Dependencies:** `redis[hiredis]` (optional, via `pip install kodosumi[redis]`)

---

### PR 3: Temporal Durable Execution ([#32](https://github.com/masumi-network/kodosumi/pull/32))

**Branch:** `feat/temporal-durability`
**Config:** `KODO_EXECUTION_MODE=temporal`, `KODO_TEMPORAL_HOST=localhost:7233`

Wraps agent job execution in Temporal workflows for crash recovery, automatic retry, and status queries.

**New/modified files:**

| File | Action | What |
|------|--------|------|
| `kodosumi/workflows.py` | **New** | `AgentWorkflow` (Temporal workflow) with `AgentJobInput`/`AgentJobResult` dataclasses, pause/resume/cancel signals, status/paused/error queries |
| `kodosumi/activities.py` | **New** | `execute_agent` activity wrapping `create_runner()` with heartbeats every 5s |
| `kodosumi/temporal_worker.py` | **New** | Worker process connecting to Temporal + Ray |
| `kodosumi/config.py` | Modified | Added `EXECUTION_MODE`, `TEMPORAL_HOST`, `TEMPORAL_NAMESPACE`, `TEMPORAL_TASK_QUEUE`, `TEMPORAL_WORKFLOW_ID_PREFIX`, `TEMPORAL_EXECUTION_TIMEOUT` |
| `kodosumi/runner/main.py` | Modified | `Launch()` branches to `_launch_temporal()` when mode is `"temporal"` |
| `kodosumi/cli.py` | Modified | Added `koco temporal-worker` command |
| `pyproject.toml` | Modified | Added `temporal = ["temporalio>=1.7.0"]` optional dep |
| `tests/test_temporal.py` | **New** | 18 unit tests covering dataclasses, workflow structure, config, CLI |

**Key design decisions:**
- Activity wraps Runner, doesn't replace it — `execute_agent()` calls `create_runner()` identically to `Launch()`
- Temporal adds durability **around** the execution boundary, not inside it
- `Launch()` remains synchronous; Temporal client runs in a separate thread via `concurrent.futures`
- Entry points are converted to strings for Temporal serialization; inputs to dicts
- Heartbeats every 5s; 120s heartbeat timeout; 3 retry attempts with exponential backoff

**How it works:**
- When `KODO_EXECUTION_MODE=direct` (default): current direct Ray actor execution, unchanged
- When set to `temporal`: `Launch()` starts an `AgentWorkflow` via Temporal client
- The workflow executes the `execute_agent` activity which creates and monitors a Ray Runner
- All event streaming, forms, locks, and tracing work identically to direct mode
- Crash recovery: if the Temporal worker dies, Temporal retries the activity on another worker

**Dependencies:** `temporalio` (optional, via `pip install kodosumi[temporal]`)

---

### PR 4: Docker Compose ([#33](https://github.com/masumi-network/kodosumi/pull/33))

**Branch:** `feat/docker-compose`

Provides containerized deployment for all configurations.

**New files:**

| File | What |
|------|------|
| `Dockerfile` | Python 3.11-slim, installs all optional deps, exposes 3370 |
| `docker-compose.yml` | Full stack: Panel + Spooler + Ray + PostgreSQL + Redis (Temporal behind `--profile temporal`) |
| `docker-compose.dev.yml` | Lightweight dev: Kodosumi + Ray only, code mounted for reload |
| `docker-compose.override.yml.example` | Template for custom secrets/config |
| `.dockerignore` | Excludes .git, __pycache__, data/, .env |
| `docs/docker.md` | Setup guide, architecture, port reference |

**Usage:**
```bash
# Lightweight dev (SQLite + Ray polling)
docker compose -f docker-compose.dev.yml up

# Full stack (PostgreSQL + Redis)
docker compose up

# Full stack + Temporal (durable execution)
docker compose --profile temporal up
```

---

## What's NOT Changed

These core modules remain completely untouched:

| Module | Description |
|--------|-------------|
| `kodosumi/serve.py` | ServeAPI — the user-facing FastAPI wrapper for agent flows |
| `kodosumi/service/auth.py` | Login/logout endpoints |
| `kodosumi/service/jwt.py` | JWT authentication middleware |
| `kodosumi/service/role.py` | User/role management |
| `kodosumi/service/flow.py` | Flow registration |
| `kodosumi/service/proxy.py` | Proxy routing + LockController |
| `kodosumi/service/health.py` | Health check endpoint |
| `kodosumi/runner/formatter.py` | Output formatting (Markdown, ANSI, YAML) |
| `kodosumi/runner/files.py` | Async/sync file system wrapper |
| `kodosumi/response.py` | Response helpers |
| `kodosumi/error.py` | Exception definitions |
| `kodosumi/log.py` | Logging configuration |
| All admin templates & static assets | Admin panel UI |

**The public API (`ServeAPI`, `Launch`, `Tracer`, `Forms`) is unchanged.** Existing agents work without modification.

---

## Backwards Compatibility

Every feature uses a **default-off** pattern:

| Setting | Default | Effect of Default |
|---------|---------|-------------------|
| `KODO_EXECUTION_DATABASE` | `None` | Per-user SQLite files (current behavior) |
| `KODO_EVENT_TRANSPORT` | `"ray"` | Ray queue polling (current behavior) |
| `KODO_EXECUTION_MODE` | `"direct"` | Direct Ray actor execution (current behavior) |

No new required dependencies are added. Optional features require explicit `pip install kodosumi[postgres]`, `kodosumi[redis]`, or `kodosumi[temporal]`.

**Upgrade path:** `pip install --upgrade kodosumi` — everything works exactly as before. New features are activated only by setting the corresponding environment variables.

---

## Architecture

### Before (upstream)

```
Launch() → Runner (Ray actor) → ray.util.queue.Queue → Spooler (polling) → SQLite files
```

### After (this fork, all features enabled)

```
Launch() → Temporal Workflow → Runner (Ray actor) → Redis Stream → Spooler (XREADGROUP) → PostgreSQL
              │                                                          │
              │ (crash recovery,                              (consumer groups,
              │  retry, signals)                               multiple spoolers)
              │
         Temporal Worker
         (heartbeats, retry)
```

### After (this fork, default config)

```
Launch() → Runner (Ray actor) → ray.util.queue.Queue → Spooler (polling) → SQLite files
```

Identical to upstream. Zero overhead from unused features.

---

## Configuration Reference

### PostgreSQL (PR 1)

```bash
# Execution events (replaces per-user SQLite files)
KODO_EXECUTION_DATABASE=postgresql+asyncpg://user:pass@localhost:5432/kodosumi

# Admin database (also supports PostgreSQL)
KODO_ADMIN_DATABASE=postgresql+asyncpg://user:pass@localhost:5432/kodosumi
```

### Redis Streams (PR 2)

```bash
KODO_EVENT_TRANSPORT=redis          # "ray" (default) or "redis"
KODO_REDIS_URL=redis://localhost:6379
KODO_REDIS_STREAM_PREFIX=kodo:events:
KODO_REDIS_CONSUMER_GROUP=kodo-spooler
KODO_REDIS_BLOCK_MS=1000            # Consumer blocking timeout
KODO_REDIS_MAX_STREAM_LEN=10000     # Per-stream max entries
```

### Temporal (PR 3)

```bash
KODO_EXECUTION_MODE=temporal        # "direct" (default) or "temporal"
KODO_TEMPORAL_HOST=localhost:7233
KODO_TEMPORAL_NAMESPACE=default
KODO_TEMPORAL_TASK_QUEUE=kodosumi-agents
KODO_TEMPORAL_WORKFLOW_ID_PREFIX=kodo-
KODO_TEMPORAL_EXECUTION_TIMEOUT=3600  # Max job duration (seconds)
```

---

## PR Merge Order

```
PR 1 (PostgreSQL)  ──┐
                     ├── PR 4 (Docker Compose)
PR 2 (Redis)  ──────┤
      │              │
      └── PR 3 (Temporal) ──┘
```

PRs 1 and 2 are fully independent. PR 3 is also independently mergeable (no code dependency on PR 2). PR 4 references all services in Docker configs.

---

## Testing

See **[TESTING.md](./TESTING.md)** for comprehensive test instructions including unit tests, integration tests, and end-to-end verification.

---

## Related Upstream Issues

- [#8 — Scale the spooler component](https://github.com/masumi-network/kodosumi/issues/8) (PRs 1, 2, 3)
- [#11 — Provide containers](https://github.com/masumi-network/kodosumi/issues/11) (PR 4)

---

## Documentation

- [TESTING.md](./TESTING.md) — How to test each PR (unit, integration, e2e)
- [docs/RESEARCH.md](./docs/RESEARCH.md) — Full platform evaluation, alternative stacks comparison
- [docs/IMPLEMENTATION_PLAN.md](./docs/IMPLEMENTATION_PLAN.md) — Original detailed implementation specification
- [docs/docker.md](./docs/docker.md) — Docker Compose setup guide (on `feat/docker-compose` branch)
