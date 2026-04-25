"""
app/metrics.py — in-memory metrics counters
============================================
Lightweight counters that track key operational stats for the /metrics
endpoint. All state lives in a single module-level instance so every part
of the app shares the same counters without passing anything around.

Why in-memory and not a database query?
  /metrics is hit by monitoring tools (UptimeRobot, Grafana) on short
  intervals. A DB query per scrape adds latency and load for no benefit —
  aggregation queries over a large messages table would be slow. In-memory
  counters are O(1) reads and writes, always fast, never a bottleneck.

  Trade-off: counters reset on process restart. For a single-instance
  deployment this is acceptable — the dashboard shows "since last deploy"
  which is still useful for spotting spikes. Multi-instance deployments
  would need Redis counters instead (a future step).

Thread safety:
  FastAPI runs on a single async event loop in one process. Coroutines are
  cooperative — only one runs at a time, so += on a plain int is safe
  without locks. If you add workers (--workers N), switch to Redis.

Usage:
  from app.metrics import metrics
  metrics.record_request(duration_ms=45.2)
  metrics.record_ai_call(tokens=320, cost_usd=0.000019)
  metrics.record_error()
"""

import time
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """All operational counters for the running process."""

    # Process start time — used to compute uptime_seconds
    _started_at: float = field(default_factory=time.monotonic)

    # Request counters
    requests_total: int = 0
    _total_duration_ms: float = 0.0  # for avg_response_time_ms

    # AI call counters
    ai_calls_total: int = 0
    ai_tokens_total: int = 0
    ai_estimated_cost_usd: float = 0.0

    # Error counter (any unhandled exception or AIServiceError)
    errors_total: int = 0

    def record_request(self, duration_ms: float) -> None:
        """Call once per completed HTTP request (success or error)."""
        self.requests_total += 1
        self._total_duration_ms += duration_ms

    def record_ai_call(self, tokens: int, cost_usd: float) -> None:
        """Call once per successful AI inference."""
        self.ai_calls_total += 1
        self.ai_tokens_total += tokens
        self.ai_estimated_cost_usd += cost_usd

    def record_error(self) -> None:
        """Call once per AIServiceError or unhandled exception."""
        self.errors_total += 1

    @property
    def uptime_seconds(self) -> float:
        return round(time.monotonic() - self._started_at, 1)

    @property
    def requests_per_minute(self) -> float:
        elapsed_minutes = (time.monotonic() - self._started_at) / 60
        if elapsed_minutes < 0.001:
            return 0.0
        return round(self.requests_total / elapsed_minutes, 1)

    @property
    def avg_response_time_ms(self) -> float:
        if self.requests_total == 0:
            return 0.0
        return round(self._total_duration_ms / self.requests_total, 1)


# Module-level singleton — import this everywhere:
#   from app.metrics import metrics
metrics = Metrics()
