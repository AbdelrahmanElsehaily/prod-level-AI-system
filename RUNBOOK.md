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

Open the Grafana Cloud dashboard:
- **AI errors panel** shows `rate(ai_errors_total[5m])` — should normally be 0.
- **AI calls panel** shows `rate(ai_calls_total[5m])` — drops to 0 if Ollama is fully down.
- Filter by the `kind` label: `unreachable` = network issue, `response_error` = Ollama returned an error.

For the raw exposition (debugging only):
```bash
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
# ai_errors_total / http_requests_total{status=~"5.."} should stop climbing
# (also visible in the Grafana dashboard within ~60s of redeploy)
```

---

## Quick reference

| Signal | First place to look |
|---|---|
| 503 from `/health` | Railway dashboard → service logs |
| AI errors spike | Sentry → latest `AIServiceError` events |
| High latency | Grafana → `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` + Langfuse traces |
| Need to roll back | Railway → Deployments → Redeploy last green |
| Postgres locked | `pg_stat_activity` query (see Scenario 1) |

---

## Grafana Cloud — initial setup (one-time)

This app exposes metrics in Prometheus text format at `GET /metrics`,
protected by the `METRICS_TOKEN` header. Grafana Cloud's hosted scraper
pulls them every 60s.

### 1. Create a Grafana Cloud account

1. Sign up at https://grafana.com (free tier is enough — 10k active series, 50 GB logs, 14-day retention)
2. Create a stack. Note your stack URL, e.g. `https://<your-stack>.grafana.net`.

### 2. Add the scrape job

1. In Grafana Cloud → **Connections → Add new connection → Hosted Prometheus metrics endpoint**
2. Click **Custom scrape job** (not "infrastructure" — we're scraping a single app endpoint).
3. Configure:
   - **Job name:** `chat-api`
   - **Target:** `<your-railway-url>` (no scheme, no path — Grafana adds them)
   - **Metrics path:** `/metrics`
   - **Scheme:** `https`
   - **Scrape interval:** `60s`
   - **Custom HTTP headers:**
     - Name: `X-Metrics-Token`
     - Value: `<your METRICS_TOKEN value>`
4. Save. Within ~2 minutes you should see your first data points.

### 3. Verify the scrape is working

Grafana Cloud → **Explore** → select your Prometheus data source → run:
```promql
up{job="chat-api"}
```
- `1` means scraping is healthy.
- `0` means Grafana reached your URL but got a non-200 (most likely 403 — wrong token).
- No data at all means Grafana never reached your URL (DNS, firewall, bad target).

### 4. Key queries for the dashboard

| Panel | PromQL |
|---|---|
| Requests/sec | `sum(rate(http_requests_total[5m]))` |
| Error rate (5xx) | `sum(rate(http_requests_total{status=~"5.."}[5m]))` |
| p95 latency (s) | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))` |
| AI calls/min | `sum(rate(ai_calls_total[5m])) * 60` |
| AI tokens/min | `sum(rate(ai_tokens_total[5m])) * 60` |
| Estimated $/hour | `sum(rate(ai_cost_usd_total[5m])) * 3600` |
| Uptime (s) | `time() - app_start_time_seconds` |

### 5. Alerts (in Grafana → Alerting → Alert rules)

Suggested starting set:
- **High AI error rate:** `sum(rate(ai_errors_total[5m])) > 0.1` for 5m → page
- **5xx spike:** `sum(rate(http_requests_total{status=~"5.."}[5m])) > 0.5` for 5m → page
- **p95 latency > 5s:** `histogram_quantile(0.95, ...) > 5` for 10m → warn
- **App down (no scrape):** `up{job="chat-api"} == 0` for 2m → page
