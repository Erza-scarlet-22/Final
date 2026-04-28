# Dummy-infra-app/dummy_app.py
#
# COMPLETE FIX:
#
#  TWO SEPARATE LOG FILES:
#    app.log          — Flask access log + startup (health checks go here, NOT shipped)
#    ssl_events.log   — ONLY error/resolution events (this is what ships to S3)
#
#  SHIPPING RULES:
#    - Background loop runs every 60s but ONLY ships if new events were written
#    - Trigger endpoint ships immediately after writing error to ssl_events.log
#    - Resolve endpoint ships immediately after writing resolution
#    - No more spurious uploads every 60s with just health check lines
#
#  LOCAL DIRECTORY:
#    Logs written to:  <app_dir>/logs/  (visible in local dev)
#    Or in ECS:        /app/Dummy-infra-app/logs/

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request, send_from_directory

from error_simulator import ErrorSimulator
from log_shipper import LogShipper

# ══════════════════════════════════════════════════════════════════════════════
# LOG DIRECTORY — two dedicated files
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_log_dir() -> str:
    """
    Priority:
    1. LOG_DIR env var (ECS task definition can override)
    2. /app/Dummy-infra-app/logs  (matches Dockerfile — appuser writable)
    3. <script_dir>/logs  (local dev fallback — always writable)
    """
    env_dir = os.getenv("LOG_DIR", "")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    container_path = "/app/Dummy-infra-app/logs"
    try:
        os.makedirs(container_path, exist_ok=True)
        probe = os.path.join(container_path, ".probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return container_path
    except (OSError, PermissionError):
        pass

    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(local_path, exist_ok=True)
    return local_path


LOG_DIR = _resolve_log_dir()

# app.log   — Flask access log (health checks, request noise). NOT shipped to S3.
APP_LOG_FILE    = os.path.join(LOG_DIR, "app.log")

# ssl_events.log — ONLY error + resolution events. This is what ships to S3.
EVENTS_LOG_FILE = os.path.join(LOG_DIR, "ssl_events.log")

# ── Application logger (writes to app.log + stdout) ───────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(APP_LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("dummy-infra-app")
logger.info("Log directory   : %s", LOG_DIR)
logger.info("App log         : %s", APP_LOG_FILE)
logger.info("Events log      : %s", EVENTS_LOG_FILE)
logger.info("RAW_LOGS_BUCKET : %s", os.getenv("RAW_LOGS_BUCKET", "(not set)"))

# ── Flask app ──────────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# Disable Flask's default werkzeug request logger so health checks
# do NOT pollute the app.log (they go to stdout via gunicorn only)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── Shared state ───────────────────────────────────────────────────────────────
_active_errors: dict = {}
_state_lock = threading.Lock()

_ssl_cert = {
    "domain":         "api.dummy-app.internal",
    "cert_arn":       None,
    "status":         "valid",
    "issued_at":      (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
    "expires_at":     (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    "days_remaining": 30,
}

# ── Simulator and shipper use ssl_events.log, NOT app.log ─────────────────────
simulator = ErrorSimulator(logger, EVENTS_LOG_FILE)
shipper   = LogShipper(logger, EVENTS_LOG_FILE)

# ── Background loop — ships ONLY when new events exist ────────────────────────
def _background_ship_loop():
    """Check every 60 s. Ship only if ssl_events.log has new content."""
    while True:
        time.sleep(60)
        if shipper.has_new_events():
            logger.info("Background ship: new events detected")
            shipper.ship()
        else:
            logger.debug("Background ship: no new events, skipping")

threading.Thread(target=_background_ship_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
@app.route("/dummy", methods=["GET"])
@app.route("/dummy/", methods=["GET"])
def index_page():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dummy/static/<path:filename>", methods=["GET"])
def dummy_static(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health", methods=["GET"])
def health():
    """ALB health check — intentionally minimal, no logging."""
    return jsonify({
        "status":  "healthy",
        "service": "dummy-infra-app",
    }), 200


@app.route("/api/dummy/status", methods=["GET"])
def api_status():
    with _state_lock:
        return jsonify({
            "active_errors": list(_active_errors.values()),
            "error_count":   len(_active_errors),
            "ssl_cert":      dict(_ssl_cert),
            "timestamp":     _now(),
        }), 200


@app.route("/api/dummy/errors", methods=["GET"])
def list_errors():
    with _state_lock:
        return jsonify({"errors": list(_active_errors.values())}), 200


# ── Trigger: write SSL error to events log + ship immediately ─────────────────
@app.route("/api/dummy/trigger-error", methods=["POST"])
def trigger_error():
    body       = request.get_json(silent=True) or {}
    error_type = body.get("error_type", "").strip()

    valid_types = [
        "ssl_expired", "ssl_expiring", "password_expired",
        "db_storage", "db_connection", "compute_overload",
    ]
    if not error_type or error_type not in valid_types:
        return jsonify({"error": f"Invalid error_type. Valid: {valid_types}"}), 400

    logger.info("Trigger: %s", error_type)

    # Write to ssl_events.log
    try:
        log_entry = simulator.generate_error(error_type)
    except Exception as exc:
        logger.error("generate_error failed: %s", exc)
        return jsonify({"error": f"Log write failed: {exc}",
                        "events_log": EVENTS_LOG_FILE}), 500

    # Update in-memory state
    with _state_lock:
        _active_errors[error_type] = {
            "type":         error_type,
            "triggered_at": _now(),
            "status":       "active",
            "log_entry":    log_entry,
        }
        if error_type == "ssl_expired":
            _ssl_cert["status"]         = "expired"
            _ssl_cert["days_remaining"] = 0
            _ssl_cert["expires_at"]     = (
                datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        elif error_type == "ssl_expiring":
            _ssl_cert["status"]         = "expiring"
            _ssl_cert["days_remaining"] = 7
            _ssl_cert["expires_at"]     = (
                datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    # Ship the events log to S3 immediately (only ssl_events.log, not app.log)
    ship_ok = shipper.ship()
    logger.info("Trigger ship result: %s → s3 key: %s", ship_ok, shipper.last_s3_key)

    return jsonify({
        "triggered":    error_type,
        "log_entry":    log_entry,
        "shipped":      ship_ok,
        "shipped_to":   shipper.last_s3_key,
        "events_log":   EVENTS_LOG_FILE,
    }), 200


# ── Resolve: write resolution to events log + ship immediately ────────────────
@app.route("/api/dummy/resolve/<error_type>", methods=["POST"])
def resolve_error(error_type):
    body    = request.get_json(silent=True) or {}
    details = body.get("details", {})

    resolution_msg = simulator.generate_resolution(error_type, details)

    with _state_lock:
        if error_type in _active_errors:
            _active_errors[error_type]["status"]      = "resolved"
            _active_errors[error_type]["resolved_at"] = _now()
            _active_errors[error_type]["details"]     = details

        if error_type in ("ssl_expired", "ssl_expiring"):
            new_arn = details.get("cert_arn", "")
            if new_arn:
                _ssl_cert["cert_arn"] = new_arn
            _ssl_cert["status"]         = "valid"
            _ssl_cert["issued_at"]      = _now()
            _ssl_cert["expires_at"]     = (
                datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
            _ssl_cert["days_remaining"] = 90

    ship_ok = shipper.ship()

    return jsonify({
        "resolved":   error_type,
        "log_entry":  resolution_msg,
        "shipped":    ship_ok,
        "shipped_to": shipper.last_s3_key,
    }), 200


# ── Log tail — shows ssl_events.log content (not app.log) ────────────────────
@app.route("/api/dummy/logs", methods=["GET"])
def get_logs():
    """
    Returns last N lines of ssl_events.log — the events-only file.
    This is what the index.html log tail displays.
    """
    try:
        n = min(max(int(request.args.get("lines", 30)), 1), 200)

        if not os.path.exists(EVENTS_LOG_FILE):
            return jsonify({
                "lines":       ["(no SSL events yet — click Trigger SSL Expired to start)"],
                "events_log":  EVENTS_LOG_FILE,
                "total_lines": 0,
            })

        with open(EVENTS_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        tail = [l.rstrip() for l in lines[-n:] if l.strip()]
        return jsonify({
            "lines":       tail,
            "total_lines": len(lines),
            "events_log":  EVENTS_LOG_FILE,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": []}), 500


@app.route("/api/dummy/ship-now", methods=["POST"])
def ship_now():
    """Force-ship ssl_events.log now regardless of whether new events exist."""
    # Temporarily reset position so we force a full upload
    original_pos = shipper._last_shipped_pos
    shipper._last_shipped_pos = 0
    success = shipper.ship()
    if not success:
        shipper._last_shipped_pos = original_pos
    if success:
        return jsonify({
            "shipped":   True,
            "s3_key":    shipper.last_s3_key,
            "log_file":  EVENTS_LOG_FILE,
        }), 200
    return jsonify({
        "shipped": False,
        "error":   "Ship failed — check RAW_LOGS_BUCKET and log file",
        "bucket":  os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "exists":  os.path.exists(EVENTS_LOG_FILE),
    }), 500


@app.route("/api/dummy/ssl-cert", methods=["GET"])
def get_ssl_cert():
    with _state_lock:
        return jsonify(dict(_ssl_cert)), 200


# ── Debug endpoint ─────────────────────────────────────────────────────────────
@app.route("/api/dummy/debug", methods=["GET"])
def debug():
    """Verify log paths, permissions, and S3 config are all correct."""
    def _check(path):
        exists = os.path.exists(path)
        size   = os.path.getsize(path) if exists else 0
        try:
            with open(path, "a") as f:
                pass
            writable = True
        except Exception:
            writable = False
        return {"path": path, "exists": exists, "bytes": size, "writable": writable}

    return jsonify({
        "log_dir":          LOG_DIR,
        "app_log":          _check(APP_LOG_FILE),
        "events_log":       _check(EVENTS_LOG_FILE),
        "shipped_up_to":    shipper._last_shipped_pos,
        "has_new_events":   shipper.has_new_events(),
        "last_s3_key":      shipper.last_s3_key or "never",
        "raw_logs_bucket":  os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "raw_logs_prefix":  os.getenv("RAW_LOGS_PREFIX", "raw-logs/"),
        "aws_region":       os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "active_errors":    list(_active_errors.keys()),
    }), 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", 5001))
    logger.info("Starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
