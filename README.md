# Chat API

A production-grade FastAPI chat service backed by Ollama (local or cloud LLMs),
built end-to-end with the operational concerns a real product needs:
observability, rate limiting, caching, CI/CD, and an incident runbook.

This is a portfolio project, but it is **not** a toy. Every layer was added
deliberately to address a problem that real services hit in production:
silent provider outages, thundering herds, leaked traces, schema drift, etc.

> **Status:** all 12 walkthrough steps merged on `develop`. See
> [`walkthrough-PLAN.md`](./walkthrough-PLAN.md) for the step-by-step build log
> and [`RUNBOOK.md`](./RUNBOOK.md) for incident playbooks.

---

## Architecture

```
                    ┌─────────────────────────────────────┐
                    │     Streamlit demo UI (ui/)         │
                    │     (separate Railway service)      │
                    └────────────────┬────────────────────┘
                                     │  HTTP
                                     ▼
┌──────────────┐    POST /chat   ┌────────────────────────────────┐
│   client     │ ──────────────▶ │       FastAPI app (app/)       │
│  (curl,      │                 │                                │
│   Streamlit) │ ◀────────────── │  middleware:                   │
└──────────────┘    JSON reply   │   • LoggingMiddleware          │
                                 │   • RateLimitMiddleware (20/min│
                                 │     per IP, sliding window)    │
                                 │                                │
                                 │  routers: /chat /health        │
                                 │           /metrics /debug      │
                                 │                                │
                                 │  services:                     │
                                 │   • chat_history (Postgres)    │
                                 │   • ai (Ollama wrapper)        │
                                 │   • cache (Redis, 1h TTL)      │
                                 └────┬───────┬───────┬───────────┘
                                      │       │       │
                  ┌───────────────────┘       │       └──────────────┐
                  │                           │                       │
                  ▼                           ▼                       ▼
           ┌─────────────┐            ┌──────────────┐         ┌──────────────┐
           │  Postgres   │            │    Redis     │         │   Ollama     │
           │ (chat hist, │            │ (rate limit, │         │ (local or    │
           │  asyncpg)   │            │  AI cache)   │         │  cloud LLM)  │
           └─────────────┘            └──────────────┘         └──────────────┘

                                 observability sinks:
                                  • Sentry          (errors)
                                  • Langfuse        (LLM traces)
                                  • Grafana Cloud   (metrics scrape /metrics)
                                  • UptimeRobot     (probe /health)
                                  • structlog       (JSON logs in prod)
```

### Layer responsibilities

| Layer | Files | Responsibility |
|---|---|---|
| HTTP | `app/main.py`, `app/routers/` | Validate requests, serialize responses. Thin — no business logic. |
| Middleware | `app/middleware/` | Cross-cutting concerns: structured logging with request IDs, rate limiting. |
| Services | `app/services/` | Business logic: chat history, AI calls, response caching. Provider-agnostic interfaces. |
| Models | `app/models/` | SQLAlchemy ORM (`database.py`) and Pydantic schemas (`schemas.py`). |
| Infra | `app/config.py`, `app/dependencies.py` | Settings (Pydantic), DB engine, Redis pool, dependency injection. |

### Key design choices (and why)

- **Ollama wrapper isolated to one file** (`app/services/ai.py`). Switching providers is a single-file change.
- **Cache key = `sha256(model + full conversation messages)`**. Preserves conversation context — never serves a stranger's reply for a similar-looking follow-up. 1h TTL.
- **Fail-open on Redis errors.** Rate limiter and cache log + skip if Redis is down — chat keeps working. The chat path never breaks because of a sidecar outage.
- **Prometheus metrics, not in-memory counters.** Works correctly across replicas: each pod exposes `/metrics`, Grafana Cloud aggregates server-side via PromQL.
- **`/metrics` is token-protected** (`X-Metrics-Token` header), and **fail-closed**: an unset token disables the endpoint entirely rather than leaving it open.
- **Migrations run before uvicorn**, not in `lifespan`. `startCommand: alembic upgrade head && exec uvicorn ...` — schema is guaranteed up-to-date before the first request, and `exec` makes uvicorn PID 1 so signals are handled correctly.
- **Conditional debug router.** `/docs` and `/debug` routes are only mounted when `ENVIRONMENT != "production"` — runtime checks would still register the route in FastAPI's table.
- **Langfuse v4 context-manager API** used for LLM traces, with hyphen-stripped UUID `trace_id`s (Langfuse calls `int(trace_id, 16)` internally).

---

## Tech stack

| Concern | Choice |
|---|---|
| Web framework | FastAPI + uvicorn |
| Database | PostgreSQL 16 (asyncpg) + SQLAlchemy 2 async + Alembic |
| Cache / rate limit | Redis 7 |
| AI provider | Ollama (local server or Ollama Cloud) |
| Logging | structlog (JSON in prod, pretty in dev) |
| Errors | Sentry |
| LLM tracing | Langfuse v4 |
| Metrics | `prometheus-client` → Grafana Cloud |
| Uptime | UptimeRobot |
| Tests | pytest, pytest-asyncio, fakeredis, httpx |
| Lint / format / types | ruff, mypy (strict), pre-commit |
| CI | GitHub Actions (lint → unit → integration → docker smoke) |
| Hosting | Railway (auto-deploy from `develop` / `main`) |
| Demo UI | Streamlit (separate Railway service) |
| Package mgmt | uv |

---

## Run it locally

### Prerequisites

- Docker + Docker Compose
- An Ollama API key for cloud models (free at https://ollama.com/settings/keys), **or** a local Ollama install with a pulled model

### One-time setup

```bash
git clone <your-fork>
cd prod-level-AI-system

# Copy env template and fill in OLLAMA_API_KEY (and any optional observability keys)
cp .env.example .env
$EDITOR .env

# Install pre-commit hooks (matches CI: ruff + mypy)
uv run --extra dev pre-commit install
```

### Start everything

```bash
docker compose up --build
```

This starts Postgres, Redis, and the API. Migrations run automatically.

### Try it

```bash
# Health check
curl http://localhost:8000/health
# {"status":"healthy","checks":{"database":"ok","redis":"ok"}}

# Send a chat message (starts a new conversation)
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is FastAPI?"}'

# Continue the conversation — pass back conversation_id from the previous response
curl -X POST http://localhost:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"And what is Pydantic?","conversation_id":"<uuid-from-previous>"}'

# Send the SAME message again — note cache_hit: true and faster response
```

OpenAPI docs at <http://localhost:8000/docs> (development only).

### Run the Streamlit demo UI

```bash
uv sync --extra ui
uv run streamlit run ui/chat_app.py
# Opens http://localhost:8501
```

---

## API surface

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Pings Postgres + Redis. 200 healthy / 503 degraded. UptimeRobot watches this. |
| `POST` | `/chat` | none (rate-limited 20/min/IP) | Send message, get AI reply. Returns `cache_hit: bool`. |
| `POST` | `/chat/docs` | none (rate-limited) | Ask a question grounded in uploaded documents via `dspy.RLM`. |
| `POST` | `/documents` | none | Upload PDF/TXT/MD (multipart). Returns document metadata. |
| `GET` | `/documents` | none | List uploaded documents (newest first). |
| `DELETE` | `/documents/{id}` | none | Delete one document. |
| `GET` | `/metrics` | `X-Metrics-Token` header | Prometheus exposition. Scraped by Grafana Cloud. |
| `GET` | `/debug/*` | none (disabled in prod) | Dev-only inspection routes. |
| `GET` | `/docs` | none (disabled in prod) | Swagger UI. |

### Document Q&A flow

`/chat/docs` is powered by `dspy.RLM` — a **recursive language model** that
loads documents into a Pyodide sandbox and lets the LLM navigate them via
a Python REPL. No vector DB, no chunking, no embeddings.

```
User uploads PDF / TXT / MD  ──▶  POST /documents
                                    │
                                    ▼
                            Extract plain text (pypdf)
                                    │
                                    ▼
                              Store in Postgres
                                    │
                                    ▼
User asks a question  ──▶  POST /chat/docs
                                    │
                                    ▼
                       Load corpus into Pyodide sandbox
                                    │
                                    ▼
                  dspy.RLM: LM writes Python to grep/slice/
                  recursively sub-query the corpus until it
                  can answer (capped at 20 sub-calls)
                                    │
                                    ▼
                            Answer + iteration count + token usage
```

**Why RLM instead of vector RAG?**
- Multi-hop questions ("how does X in doc A relate to Y in doc B?") work natively
- No embedding model or vector index to maintain
- Long documents are not artificially chunked
- Trade-off: many LM sub-calls per question (capped); not cacheable

---

## Tests

```bash
# Unit tests — fully mocked, no external services, ~1s
uv run --extra dev pytest tests/unit/ -v

# Integration tests — needs Postgres + Redis (use docker-compose)
uv run --extra dev pytest tests/integration/ -v

# Lint + format + types (same checks as CI)
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
uv run --extra dev mypy --no-incremental app/
```

CI runs all four jobs (lint → unit → integration → docker smoke build) on every PR
to `develop` or `main`. See `.github/workflows/ci.yml`.

---

## Deploy

Railway watches `develop` (staging) and `main` (production) and auto-deploys on push.
Per-environment env vars (`DATABASE_URL`, `REDIS_URL`, `OLLAMA_API_KEY`,
`SENTRY_DSN`, `LANGFUSE_*`, `METRICS_TOKEN`) are configured in the Railway dashboard.

The `startCommand` in `railway.toml`:

```
sh -c 'echo MIGRATIONS_START && alembic upgrade head && echo MIGRATIONS_DONE && exec uvicorn app.main:app --host 0.0.0.0 --port $PORT'
```

— migrations run first, then uvicorn replaces the shell as PID 1 so Railway's
process management and signal handling work correctly.

A health check on `/health` (60s timeout) gates traffic switch — a deploy whose
new image fails health-check is rolled back automatically.

---

## Observability

| Signal | Where | Notes |
|---|---|---|
| Errors | Sentry | Auto-tagged with release SHA. `AIServiceError` is the most common signal for upstream Ollama outages. |
| LLM traces | Langfuse | Per-conversation traces (trace_id = conversation UUID), with input, output, tokens, cost. |
| Metrics | Grafana Cloud | Scrapes `/metrics` every 60s. PromQL recipes in `RUNBOOK.md`. |
| Uptime | UptimeRobot | Pings `/health` every 5 min from multiple regions. |
| Logs | Railway → stdout | JSON in production (structlog), grep-friendly fields: `request_id`, `conversation_id`, `duration_ms`. |

Operational playbooks for `/health` 503s, AI error spikes, deploy rollbacks,
and stuck cache entries are in [`RUNBOOK.md`](./RUNBOOK.md).

---

## Repository layout

```
app/
├── main.py             FastAPI app, lifespan, middleware wiring
├── config.py           Pydantic Settings — single source of env truth
├── dependencies.py     get_db, get_redis (DI for routes)
├── logging_config.py   structlog setup
├── sentry.py           init_sentry()
├── langfuse_client.py  module-level Langfuse client
├── metrics.py          Prometheus Counters / Histograms / Gauges
├── middleware/         logging, rate_limit
├── routers/            chat, health, metrics, debug, documents
├── services/           chat_history, ai, cache, documents, rag
└── models/             database (SQLAlchemy ORM), schemas (Pydantic)

migrations/             Alembic
tests/unit/             ~90 unit tests, fully mocked
tests/integration/      slot for tests against real Postgres+Redis
ui/                     Streamlit demo (separate Railway service)
RUNBOOK.md              Incident playbooks
walkthrough-PLAN.md     12-step build log
```

---

## Cost (solo developer)

| Item | Cost |
|---|---|
| Railway (app + Postgres + Redis) | ~$5–10/mo |
| Ollama Cloud (light usage) | ~$0–5/mo |
| Sentry / Grafana Cloud / UptimeRobot / Langfuse free tiers | $0 |
| **Total** | **~$5–15/mo** |

---

## License

MIT (see `LICENSE`).
