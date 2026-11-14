"""Central configuration. Every value can be overridden with an environment variable."""
import os


def _env(name, default):
    return type(default)(os.environ.get(name, default))


PORT = _env("PORT", 8000)
DB_PATH = os.environ.get("DB_PATH", "urbancart.db")
LEGACY_MODE = os.environ.get("LEGACY_MODE", "0") == "1"   # "before" state: no queue, no fallback

MIN_WORKERS = _env("MIN_WORKERS", 2)
MAX_WORKERS = _env("MAX_WORKERS", 32)
BASELINE_USERS = _env("BASELINE_USERS", 20)                # "normal load" = concurrent shoppers
GATEWAY_MAX_CONCURRENT = _env("GATEWAY_MAX_CONCURRENT", 60)  # 3x normal load: the case's scaling cap

MONITOR_INTERVAL = _env("MONITOR_INTERVAL", 2.0)           # seconds between health checks
LATENCY_ALERT_MS = _env("LATENCY_ALERT_MS", 300)
ERROR_RATE_ALERT = _env("ERROR_RATE_ALERT", 0.20)
QUEUE_ALERT_DEPTH = _env("QUEUE_ALERT_DEPTH", 500)
CONSECUTIVE_FAILS = _env("CONSECUTIVE_FAILS", 2)           # tuned thresholds -> less alert fatigue

SLA_SCALE = _env("SLA_SCALE", 1.0)                         # set 0.01 to watch escalation in a demo
SLA_MINUTES = {"P1": 30, "P2": 120, "P3": 480, "P4": 1440}
ESCALATE_L2_AT = 0.5                                       # fraction of SLA elapsed
ESCALATE_L3_AT = 0.8

BASELINE_MISMATCH_RATE = _env("BASELINE_MISMATCH_RATE", 0.10)  # student assumption (legacy state)
