"""Prometheus metrics shared by all services (scraped from /metrics, which the
ingress does not expose publicly)."""
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "agency_http_requests_total", "HTTP requests", ["service", "method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "agency_http_request_seconds", "HTTP request latency", ["service", "method", "route"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
AGENT_RUNS = Counter("agency_agent_runs_total", "Agent runs", ["topic", "status"])
AGENT_RUN_LATENCY = Histogram(
    "agency_agent_run_seconds", "End-to-end agent run latency", ["topic"],
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300),
)
LLM_CALLS = Counter(
    "agency_llm_calls_total", "LLM calls via model gateway", ["topic", "tier", "model", "status"]
)
LLM_LATENCY = Histogram(
    "agency_llm_call_seconds", "LLM call latency", ["tier", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120),
)
LLM_TOKENS = Counter("agency_llm_tokens_total", "LLM tokens", ["topic", "model", "direction"])
LLM_COST = Counter("agency_llm_cost_usd_total", "LLM cost in USD", ["topic", "model"])
ROUTING_DECISIONS = Counter(
    "agency_model_routing_total", "Model tier routing decisions", ["source", "tier"]
)
QUOTA_REJECTIONS = Counter("agency_quota_rejections_total", "Requests refused by plan/quota", ["topic", "code"])
CACHE_EVENTS = Counter("agency_cache_events_total", "Cache lookups", ["layer", "topic", "result"])
PII_REDACTIONS = Counter("agency_pii_redactions_total", "PII entities redacted before LLM calls", ["entity"])
FEEDBACK = Counter("agency_feedback_total", "User feedback on runs", ["topic", "score"])
CANARY_CALLS = Counter("agency_canary_calls_total", "LLM calls served by a canary model", ["tier", "model", "status"])

# Quality gauges, refreshed from Postgres by the edge gateway (drift + online evals),
# so Prometheus alerting covers quality regressions, not just uptime.
QUALITY_SCORE = Gauge("agency_quality_judge_score", "Mean online LLM-judge score (1-5), last 24h", ["topic"])
DRIFT_FLAG = Gauge("agency_drift_detected", "1 if the latest drift report flagged this metric", ["topic", "metric"])
EVAL_PASS_RATE = Gauge("agency_eval_pass_rate", "Pass rate of the latest offline eval run", ["topic"])
