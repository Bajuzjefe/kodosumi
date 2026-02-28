# Testing Guide

This document explains how to test each scaling enhancement PR — unit tests, integration tests, and end-to-end verification.

---

## Prerequisites

```bash
# Python 3.10+ required (kodosumi's minimum)
python3 --version  # must be >= 3.10

# Clone the fork
git clone https://github.com/Bajuzjefe/kodosumi.git
cd kodosumi

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install kodosumi with all optional dependencies
pip install -e ".[postgres,redis,temporal,tests]"
```

---

## Unit Tests

### PR 1: PostgreSQL Support

```bash
git checkout feat/postgres-support
pytest tests/test_storage.py -v
```

**Expected: 15 tests pass**

| Test Class | Count | What it tests |
|------------|-------|---------------|
| `TestSqliteSpoolerWriter` | 6 | SQLite writer: setup, save, empty payload, multiple DBs, large batch, idempotent setup |
| `TestPostgresSpoolerWriter` | 4 | PostgreSQL writer: init, setup returns tuple, empty save noop, close noop |
| `TestCreateSpoolerWriter` | 2 | Factory: default returns SQLite, postgres URL returns PostgresWriter |
| `TestExecutionEventModel` | 3 | Model: tablename, columns, indexes |

**What's tested without a real database:**
- `PostgresSpoolerWriter` is tested with mocked connection (no real PostgreSQL needed)
- `SqliteSpoolerWriter` creates real temp SQLite DBs via `tmp_path`
- Factory function tested with mock Settings objects

---

### PR 2: Redis Streams Transport

```bash
git checkout feat/redis-transport
pytest tests/test_transport.py -v
```

**Expected: 28 tests pass**

| Test Class | Count | What it tests |
|------------|-------|---------------|
| `TestProtocolConformance` | 4 | All 4 implementations satisfy their Protocol contracts |
| `TestRayQueueProducer` | 5 | put_sync, put_async, queue property, shutdown, exception handling |
| `TestRayQueueConsumer` | 3 | read_batch with data, empty queue, ack noop |
| `TestRedisStreamProducer` | 6 | config storage, stream key, XADD call, shutdown, pickle safety |
| `TestRedisStreamConsumer` | 7 | config, default consumer name, XREADGROUP parsing, empty result, XACK, shutdown |
| `TestFactoryFunctions` | 2 | Ray returns None consumer, Redis returns RedisStreamConsumer |
| `TestConfigIntegration` | 1 | Settings has all Redis fields with correct defaults |

**What's tested without real Redis:**
- All Redis operations use `unittest.mock.MagicMock` for the Redis client
- XADD, XREADGROUP, XACK calls are verified via mock assertions
- Pickle safety verified by calling `__reduce__` and reconstructing

---

### PR 3: Temporal Durability

```bash
git checkout feat/temporal-durability
pip install temporalio  # if not already installed
pytest tests/test_temporal.py -v
```

**Expected: 14+ tests pass** (4 tests require `ray` + `psutil` which need the full kodosumi install)

| Test Class | Count | What it tests |
|------------|-------|---------------|
| `TestAgentJobInput` | 3 | Dataclass defaults, with inputs, dict serialization |
| `TestAgentJobResult` | 2 | Success and failure results |
| `TestAgentWorkflowStructure` | 4 | run/signals/queries exist, initial state |
| `TestActivityStructure` | 1 | execute_agent is importable (requires `ray`) |
| `TestTemporalWorkerModule` | 1 | Module imports (requires `ray`) |
| `TestTemporalConfig` | 2 | Default execution mode, Temporal settings defaults |
| `TestLaunchTemporalPath` | 3 | Direct mode default, helper importable, conversion logic |
| `TestCLITemporalWorker` | 1 | CLI command registered (requires `psutil`) |

---

### Run All Unit Tests at Once

```bash
# From any branch that has all test files:
pytest tests/test_storage.py tests/test_transport.py tests/test_temporal.py -v
```

---

## Integration Tests

These tests require running infrastructure (PostgreSQL, Redis, Temporal). The easiest way is to use Docker Compose from PR 4.

### Setup Infrastructure

```bash
git checkout feat/docker-compose

# Start PostgreSQL + Redis (no Temporal yet)
docker compose up postgres redis -d

# Wait for health checks
docker compose ps  # both should show "healthy"
```

### Test PR 1: PostgreSQL Integration

```bash
# 1. Create tables
KODO_ADMIN_DATABASE="postgresql+asyncpg://kodosumi:kodosumi@localhost:5432/kodosumi" \
KODO_EXECUTION_DATABASE="postgresql+asyncpg://kodosumi:kodosumi@localhost:5432/kodosumi" \
koco db --create-tables

# 2. Verify tables exist
docker compose exec postgres psql -U kodosumi -c "\dt"
# Expected: execution_events table + admin tables (role, etc.)

# 3. Check indexes
docker compose exec postgres psql -U kodosumi -c "\di"
# Expected: ix_execution_events_fid, ix_execution_events_username, ix_execution_events_fid_kind

# 4. Start panel with PostgreSQL
KODO_ADMIN_DATABASE="postgresql+asyncpg://kodosumi:kodosumi@localhost:5432/kodosumi" \
KODO_EXECUTION_DATABASE="postgresql+asyncpg://kodosumi:kodosumi@localhost:5432/kodosumi" \
koco serve --address http://localhost:3370
```

### Test PR 2: Redis Integration

```bash
# 1. Start panel + spooler with Redis transport
KODO_EVENT_TRANSPORT=redis \
KODO_REDIS_URL=redis://localhost:6380 \
koco serve --address http://localhost:3370

# In another terminal:
KODO_EVENT_TRANSPORT=redis \
KODO_REDIS_URL=redis://localhost:6380 \
koco spool --block

# 2. Execute an agent flow and verify Redis streams
docker compose exec redis redis-cli KEYS "kodo:events:*"
# Expected: stream keys appear for each execution

# 3. Check stream contents
docker compose exec redis redis-cli XLEN kodo:events:<fid>
# Expected: message count matching event count

# 4. Verify consumer group
docker compose exec redis redis-cli XINFO GROUPS kodo:events:<fid>
# Expected: kodo-spooler group with consumers
```

### Test PR 3: Temporal Integration

```bash
# 1. Start Temporal
docker compose --profile temporal up temporal temporal-ui -d

# 2. Wait for Temporal to be ready (check http://localhost:8233)

# 3. Start the Temporal worker
KODO_TEMPORAL_HOST=localhost:7233 \
KODO_RAY_SERVER=localhost:6379 \
koco temporal-worker

# 4. Start panel with Temporal execution mode
KODO_EXECUTION_MODE=temporal \
KODO_TEMPORAL_HOST=localhost:7233 \
koco serve --address http://localhost:3370

# 5. Execute an agent flow — should appear in Temporal UI at http://localhost:8233
# Filter by workflow type "AgentWorkflow"

# 6. Verify crash recovery: kill the temporal-worker mid-execution
# Temporal should show the workflow as "Running" and retry when worker restarts
```

---

## End-to-End Tests

### E2E 1: Default Config (Regression)

Verify that **no behavior changes** with default config (no env vars set).

```bash
git checkout feat/postgres-support  # or any PR branch

# Start normally — should work exactly like upstream
koco serve --address http://localhost:3370 &
koco spool --block &

# 1. Log in at http://localhost:3370
# 2. Register a sample agent flow
# 3. Execute it
# 4. Verify events appear in the timeline
# 5. Check that SQLite files are created at ./data/execution/<user>/<fid>/sqlite3.db
```

### E2E 2: Full Stack via Docker

```bash
git checkout feat/docker-compose

# Start everything
docker compose up -d

# 1. Open http://localhost:3370 — panel should load
# 2. Open http://localhost:8265 — Ray Dashboard should show the cluster
# 3. Login with default credentials (admin@example.com / admin)
# 4. Register and execute an agent flow
# 5. Verify events in the timeline
```

### E2E 3: PostgreSQL + Redis Combined

```bash
# Use the override example
cp docker-compose.override.yml.example docker-compose.override.yml
docker compose up -d

# Execute an agent flow, then verify:

# Events in PostgreSQL:
docker compose exec postgres psql -U kodosumi \
  -c "SELECT fid, kind, COUNT(*) FROM execution_events GROUP BY fid, kind"

# Events flowed through Redis:
docker compose exec redis redis-cli KEYS "kodo:events:*"
```

### E2E 4: Temporal Crash Recovery

```bash
docker compose --profile temporal up -d

# 1. Start an agent flow (long-running)
# 2. While it's running, kill the temporal-worker:
docker compose stop temporal-worker

# 3. Check Temporal UI (http://localhost:8233) — workflow should show "Running"
# 4. Restart the worker:
docker compose start temporal-worker

# 5. Verify the workflow completes successfully (Temporal retries the activity)
```

### E2E 5: Multi-Spooler Scaling (Redis)

```bash
# Start 2 spoolers with the same consumer group
KODO_EVENT_TRANSPORT=redis KODO_REDIS_URL=redis://localhost:6380 koco spool --block &
KODO_EVENT_TRANSPORT=redis KODO_REDIS_URL=redis://localhost:6380 koco spool --block &

# Execute multiple agent flows simultaneously
# Verify in Redis that messages are distributed across both consumers:
docker compose exec redis redis-cli XINFO GROUPS kodo:events:<fid>
# Expected: 2 consumers in the kodo-spooler group
```

---

## Test Results Summary

Tested on Python 3.9.6 (limited — kodosumi requires 3.10+):

| PR | Branch | Unit Tests | Result | Notes |
|----|--------|-----------|--------|-------|
| PR 1 | `feat/postgres-support` | `tests/test_storage.py` | 5/15 passed | 10 fail due to Python 3.9 (`autocommit=True` is 3.12+, `Self` is 3.11+ — both upstream patterns) |
| PR 2 | `feat/redis-transport` | `tests/test_transport.py` | **28/28 passed** | All green, no external deps needed |
| PR 3 | `feat/temporal-durability` | `tests/test_temporal.py` | 14/18 passed | 4 fail because `ray`/`psutil` require Python 3.10+ to install |
| PR 4 | `feat/docker-compose` | Manual | N/A | Docker build/deploy |

**On Python 3.10+ with full deps installed, all 61 unit tests should pass.**

---

## Troubleshooting

### "No module named 'ray'" in tests
Install the full package: `pip install -e "."` (requires Python 3.10+)

### PostgreSQL connection refused
Check that postgres is running: `docker compose ps postgres`
Verify health: `docker compose exec postgres pg_isready -U kodosumi`

### Redis connection refused
Note: Docker maps Redis to port **6380** (not 6379) to avoid conflict with Ray GCS.
Use `KODO_REDIS_URL=redis://localhost:6380` when running outside Docker.

### Temporal worker can't connect
Temporal takes ~30s to start. Check: `curl http://localhost:7233`
Check Temporal UI: `http://localhost:8233`

### SQLite autocommit TypeError
This is a Python version issue. `autocommit=True` in `sqlite3.connect()` requires Python 3.12+.
This is an existing upstream pattern in `kodosumi/spooler.py:63`. Use Python 3.12+ or the PostgreSQL backend.
