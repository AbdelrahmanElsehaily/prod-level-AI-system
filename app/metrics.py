"""
app/metrics.py — Prometheus metrics primitives
==============================================
Defines the Counter / Histogram / Gauge instances that the app updates as
it runs. The /metrics router (app/routers/metrics.py) renders these into
the Prometheus text format that Grafana Cloud's hosted scraper consumes.

Why prometheus-client and not in-memory counters?
  In-memory counters live in one process and reset on restart. Prometheus
  primitives are still per-process, but the Prometheus ecosystem is built
  to handle that: Grafana Cloud scrapes every replica's /metrics endpoint
  and aggregates server-side. So the SAME code works whether you run 1
  replica or 50 — Grafana sums them across replicas at query time.

Metric types — quick primer
---------------------------
Counter:   monotonically increasing number. Reset only on process restart.
           Use for "total events" — requests, errors, tokens.
           Query rate(...) in Grafana to get "per-second" rate.

Histogram: distribution of values bucketed by ranges. Use for durations.
           Lets Grafana compute p50/p95/p99 latencies via histogram_quantile().
           NEVER use a single average — it hides outliers.

Gauge:     value that goes up AND down. Use for "current state" (queue depth,
           memory usage). We use it for the process start time → uptime.

Naming convention (per Prometheus best practice)
------------------------------------------------
  <namespace>_<subsystem>_<name>_<unit>
  - units always SI base: seconds (not ms), bytes (not MB)
  - counters end in _total
  - all_lowercase_with_underscores

Usage
-----
    from app.metrics import requests_total, request_duration_seconds

    requests_total.labels(method="GET", path="/health", status="200").inc()
    request_duration_seconds.labels(method="GET", path="/health").observe(0.043)
"""

import time

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# HTTP request metrics — populated by app/middleware/logging.py
# ---------------------------------------------------------------------------

# Total HTTP requests, broken down by method, path, and status code.
# Labels let Grafana slice by any combination, e.g.:
#   sum(rate(http_requests_total{status=~"5.."}[5m]))  → server-error rate
#
# WARNING: every unique label combination creates a new time series.
# We use `path` not the raw request URL — never use IDs/UUIDs as labels
# (would explode cardinality). The middleware passes `request.url.path`
# which is the route template, e.g. "/chat" not "/chat?msg=hi".
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the app.",
    labelnames=("method", "path", "status"),
)

# Histogram of request durations in SECONDS (Prometheus convention — never ms).
# Default buckets [.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10] cover the
# typical web-app range (5ms … 10s). Override if your latencies skew differently.
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path"),
)

# ---------------------------------------------------------------------------
# AI / Ollama metrics — populated by app/services/ai.py
# ---------------------------------------------------------------------------

# Successful AI inference calls.
ai_calls_total = Counter(
    "ai_calls_total",
    "Total successful AI inference calls.",
    labelnames=("model",),
)

# Total tokens consumed (input + output combined).
# Use rate() in Grafana to see tokens/second; sum over time for the bill.
ai_tokens_total = Counter(
    "ai_tokens_total",
    "Total tokens consumed across all AI calls (input + output).",
    labelnames=("model",),
)

# Estimated USD cost. Float counter — Prometheus supports non-integer counters.
ai_cost_usd_total = Counter(
    "ai_cost_usd_total",
    "Estimated cumulative cost of AI calls in USD.",
    labelnames=("model",),
)

# AI errors (Ollama down, model not found, response error).
# Separate from generic HTTP errors so we can alert specifically on
# "AI provider is broken" vs "the app itself is broken".
ai_errors_total = Counter(
    "ai_errors_total",
    "Total AI inference errors (Ollama unreachable, model errors, etc.).",
    labelnames=("model", "kind"),
)

# ---------------------------------------------------------------------------
# Process metrics
# ---------------------------------------------------------------------------

# Unix-time of process start. We expose start time as a Gauge — Grafana
# computes uptime as `time() - process_start_time_seconds` at query time,
# which is more accurate than us recomputing it on every scrape.
#
# Note: prometheus-client also auto-registers a `process_start_time_seconds`
# default collector, but the value depends on the platform; we set our own
# explicitly so behavior is identical everywhere.
app_start_time_seconds = Gauge(
    "app_start_time_seconds",
    "Unix timestamp when the app process started.",
)
app_start_time_seconds.set(time.time())
