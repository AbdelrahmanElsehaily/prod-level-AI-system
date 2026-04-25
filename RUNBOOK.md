# Runbook

Operational guide for the Chat API. Written for 2am incidents — steps are
explicit and sequential. No assumed context.

---

## Scenario 1 — `GET /health` returns 503

The health endpoint checks Postgres and Redis. A 503 means one or both are down.

### Diagnose

```bash
# See which check failed
curl https://<your-railway-url>/health
# Response shows: {"checks": {"database": "error: ...", "redis": "ok"}}
```

### Database is down

1. Open Railway dashboard → your project → Postgres service
2. Check the service is running (green). If crashed, click **Restart**.
3. Check **Connect** tab — verify `DATABASE_URL` is set on the app service.
   If missing, add the Postgres service variable reference and redeploy.
4. If Postgres is healthy but the app can't connect, check for a recent
   migration failure in the deploy logs:
   ```
   Railway dashboard → app service → Deployments → latest → View logs
   ```
5. If a migration is stuck, connect directly and check for locks:
   ```bash
   # Railway provides a psql connect command in the Postgres service Connect tab
   SELECT pid, query, state FROM pg_stat_activity WHERE state = 'idle in transaction';
   # Kill a stuck connection:
   SELECT pg_terminate_backend(<pid>);
   ```

### Redis is down

1. Open Railway dashboard → Redis service → check it is running.
2. If crashed, click **Restart**.
3. Verify `REDIS_URL` is set on the app service. If missing, add the Redis
   variable reference and redeploy.
4. Note: Redis is used for rate limiting only. If Redis is down and the app
   is configured to fail open (it is — see `rate_limit.py`), requests still
   go through. A Redis outage degrades rate limiting but does not break chat.

---

## Scenario 2 — AI error rate spikes

Signs: `errors_total` rising in `/metrics`, users report "AI unavailable" errors,
Sentry shows `AIServiceError` events.

### Diagnose

```bash
# Check current error count and AI call rate
curl -H "X-Metrics-Token: <token>" https://<your-railway-url>/metrics
```

Check Sentry for the error message — it will tell you exactly what Ollama returned.

### Ollama cloud is down or rate-limited

1. Check https://status.ollama.com for incidents.
2. If rate-limited: reduce load or wait. Ollama cloud limits vary by plan.
3. If the model was removed or renamed: update `OLLAMA_MODEL` in Railway
   environment variables and redeploy.

### Wrong API key

Sentry will show: `AIServiceError: AI model error: unauthorized`

1. Go to https://ollama.com/settings/keys — verify the key exists and is active.
2. Update `OLLAMA_API_KEY` in Railway environment variables.
3. Redeploy (Railway picks up env var changes without a code push via
   **Deployments → Redeploy**).

### Model not available locally

If running local Ollama: `AIServiceError: AI model error: model not found`

```bash
ollama pull llama3.2   # or whatever OLLAMA_MODEL is set to
```

---

## Scenario 3 — Rolling back a bad deployment on Railway

Use this when a deploy caused regressions and you need to restore the last
known-good version immediately.

### Instant rollback (no code change needed)

1. Railway dashboard → app service → **Deployments** tab
2. Find the last green (healthy) deployment
3. Click the **⋯** menu → **Redeploy**
4. Railway spins up the old image. The health check must pass before traffic
   switches — if the old version is also broken, Railway won't switch.

### Rollback via git revert (preferred for permanent fix)

```bash
git checkout develop
git revert <bad-commit-sha>   # creates a new revert commit
git push origin develop       # triggers Railway deploy automatically
```

This is safer than force-pushing because it keeps history intact and goes
through the normal CI + health check gate.

### Verify rollback succeeded

```bash
curl https://<your-railway-url>/health
# Should return 200 {"status": "healthy"}

curl -H "X-Metrics-Token: <token>" https://<your-railway-url>/metrics
# errors_total should stop climbing
```

---

## Quick reference

| Signal | First place to look |
|---|---|
| 503 from `/health` | Railway dashboard → service logs |
| AI errors spike | Sentry → latest `AIServiceError` events |
| High latency | `/metrics` → `avg_response_time_ms` + Langfuse traces |
| Need to roll back | Railway → Deployments → Redeploy last green |
| Postgres locked | `pg_stat_activity` query (see Scenario 1) |
