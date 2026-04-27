# Dummy-infra-app/dummy_app.py
#
# FIXES:
#   1. LOG_DIR corrected to /app/Dummy-infra-app/logs to match Dockerfile
#      (Dockerfile creates /app/Dummy-infra-app/logs and sets appuser perms there)
#   2. Added /dummy and /dummy/ routes for ALB path-based routing
#   3. Local dev fallback: if /app/Dummy-infra-app/logs not writable, uses ./logs

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

import boto3
from flask import Flask, jsonify, request, send_from_directory

from error_simulator import ErrorSimulator
from log_shipper import LogShipper

# ── Resolve log directory ──────────────────────────────────────────────────────
# Dockerfile.dummy-app:
#   WORKDIR /app
#   COPY Dummy-infra-app/ ./Dummy-infra-app/
#   RUN mkdir -p /app/Dummy-infra-app/logs && chown -R appuser:appgroup /app
#   CMD cd /app/Dummy-infra-app && gunicorn app:app
#
# So the process CWD is /app/Dummy-infra-app and appuser owns that tree.
# We must write logs inside /app/Dummy-infra-app/logs, NOT /app/logs.
# ENV var LOG_DIR overrides everything (set in ECS task definition if needed).

def _resolve_log_dir() -> str:
    # 1. Explicit env override
    env_dir = os.getenv("LOG_DIR", "")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    # 2. Preferred container path (matches Dockerfile)
    container_path = "/app/Dummy-infra-app/logs"
    try:
        os.makedirs(container_path, exist_ok=True)
        test = os.path.join(container_path, ".write_test")
        with open(test, "w") as f:
            f.write("ok")
        os.remove(test)
        return container_path
    except (OSError, PermissionError):
        pass

    # 3. Local development fallback (relative to CWD)
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(local_path, exist_ok=True)
    return local_path


LOG_DIR  = _resolve_log_dir()
LOG_FILE = os.path.join(LOG_DIR, "dummy-app.log")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("dummy-infra-app")
logger.info("Log directory: %s", LOG_DIR)
logger.info("Log file: %s", LOG_FILE)

# ── Flask app ──────────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# ── Shared state ───────────────────────────────────────────────────────────────
_active_errors: dict = {}
_state_lock = threading.Lock()

# ── SSL certificate simulation state ──────────────────────────────────────────
_ssl_cert = {
    "domain":         "api.dummy-app.internal",
    "cert_arn":       None,
    "status":         "valid",
    "issued_at":      (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(),
    "expires_at":     (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    "days_remaining": 30,
}

simulator = ErrorSimulator(logger, LOG_FILE)
shipper   = LogShipper(logger, LOG_FILE)

# Write a startup log line so the file exists immediately on first ship
with open(LOG_FILE, "a", encoding="utf-8") as _f:
    _f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}] "
             f"[INFO] dummy-infra-app started. Log dir: {LOG_DIR}\n")

# ── Background: ship logs every 60 s ──────────────────────────────────────────
def _shipping_loop():
    while True:
        time.sleep(60)
        shipper.ship()

threading.Thread(target=_shipping_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def index_root():
    """Direct root access — serves index.html."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dummy", methods=["GET"])
@app.route("/dummy/", methods=["GET"])
def index_dummy():
    """ALB path-pattern /dummy → this route. Serves index.html."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dummy/static/<path:filename>", methods=["GET"])
def dummy_static(filename):
    """Static files resolved relative to /dummy path."""
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health", methods=["GET"])
def health():
    """ALB target group health check. Returns log file status for debugging."""
    log_file_exists = os.path.exists(LOG_FILE)
    log_file_size   = os.path.getsize(LOG_FILE) if log_file_exists else 0
    return jsonify({
        "status":           "healthy",
        "service":          "dummy-infra-app",
        "active_errors":    len(_active_errors),
        "raw_logs_bucket":  os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "last_ship":        shipper.last_s3_key or "never",
        "log_dir":          LOG_DIR,
        "log_file":         LOG_FILE,
        "log_file_exists":  log_file_exists,
        "log_file_bytes":   log_file_size,
    }), 200


@app.route("/api/dummy/status", methods=["GET"])
def status():
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

    logger.info("Trigger requested: %s", error_type)

    try:
        log_entry = simulator.generate_error(error_type)
    except Exception as exc:
        logger.error("generate_error failed: %s", exc)
        return jsonify({"error": f"Log generation failed: {exc}"}), 500

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

    # Ship immediately after trigger
    ship_ok = shipper.ship()
    logger.info("Ship after trigger: %s → %s", ship_ok, shipper.last_s3_key)

    return jsonify({
        "triggered":  error_type,
        "log_entry":  log_entry,
        "shipped":    ship_ok,
        "shipped_to": shipper.last_s3_key,
        "log_file":   LOG_FILE,
    }), 200


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

    shipper.ship()

    return jsonify({
        "resolved":   error_type,
        "log_entry":  resolution_msg,
        "shipped_to": shipper.last_s3_key,
    }), 200


@app.route("/api/dummy/logs", methods=["GET"])
def get_logs():
    """Return recent log lines. Also shows log file path for debugging."""
    try:
        n = min(max(int(request.args.get("lines", 30)), 1), 200)
        if not os.path.exists(LOG_FILE):
            return jsonify({
                "lines": [f"(log file not yet created at {LOG_FILE})"],
                "log_file": LOG_FILE,
            })
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = [l.rstrip() for l in lines[-n:] if l.strip()]
        return jsonify({
            "lines":       tail,
            "total_lines": len(lines),
            "log_file":    LOG_FILE,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": [], "log_file": LOG_FILE}), 500


@app.route("/api/dummy/ship-now", methods=["POST"])
def ship_now():
    success = shipper.ship()
    if success:
        return jsonify({"shipped": True, "s3_key": shipper.last_s3_key}), 200
    return jsonify({
        "shipped": False,
        "error":   "Ship failed — check RAW_LOGS_BUCKET env var and log file",
        "log_file": LOG_FILE,
        "log_file_exists": os.path.exists(LOG_FILE),
        "bucket": os.getenv("RAW_LOGS_BUCKET", "(not set)"),
    }), 500


@app.route("/api/dummy/ssl-cert", methods=["GET"])
def get_ssl_cert():
    with _state_lock:
        return jsonify(dict(_ssl_cert)), 200


# ── Debug endpoint — check log file exists and is writable ────────────────────
@app.route("/api/dummy/debug", methods=["GET"])
def debug():
    """Debug endpoint to verify log file path, bucket config, and permissions."""
    exists   = os.path.exists(LOG_FILE)
    writable = False
    if exists:
        try:
            with open(LOG_FILE, "a") as f:
                pass
            writable = True
        except Exception:
            pass
    return jsonify({
        "log_dir":         LOG_DIR,
        "log_file":        LOG_FILE,
        "log_file_exists": exists,
        "log_file_size":   os.path.getsize(LOG_FILE) if exists else 0,
        "log_writable":    writable,
        "raw_logs_bucket": os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "raw_logs_prefix": os.getenv("RAW_LOGS_PREFIX", "raw-logs/"),
        "aws_region":      os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "last_ship":       shipper.last_s3_key or "never",
        "active_errors":   list(_active_errors.keys()),
    }), 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", 5001))
    logger.info("dummy-infra-app starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
