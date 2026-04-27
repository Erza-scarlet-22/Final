# Dummy-infra-app/dummy_app.py
#
# FIXES in this version:
#   1. Added /dummy route  → serves index.html (ALB routes /dummy → this app)
#   2. Added /dummy/static → serves static files under /dummy prefix
#   3. All /api/dummy/* routes unchanged
#   4. /health unchanged

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

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_DIR  = os.getenv("LOG_DIR", "/app/logs")
LOG_FILE = os.path.join(LOG_DIR, "dummy-app.log")
os.makedirs(LOG_DIR, exist_ok=True)

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

# ── Flask app ──────────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
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

# ── Background: ship logs every 60 s ──────────────────────────────────────────
def _shipping_loop():
    while True:
        time.sleep(60)
        shipper.ship()

threading.Thread(target=_shipping_loop, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── Root path (direct container / health check access) ────────────────────────
@app.route("/", methods=["GET"])
def index_root():
    return send_from_directory(STATIC_DIR, "index.html")


# ── /dummy and /dummy/  (ALB listener rule sends these here) ──────────────────
# The ALB rule matches /dummy and /dummy/* and forwards to port 5001.
# Flask receives the full original path so we must register /dummy explicitly.
@app.route("/dummy", methods=["GET"])
@app.route("/dummy/", methods=["GET"])
def index_dummy():
    """Serve index.html when accessed via the ALB /dummy path rule."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/dummy/static/<path:filename>", methods=["GET"])
def dummy_static(filename):
    """
    Serve static files when the browser resolves them relative to /dummy.
    e.g. /dummy/static/app.js → serves static/app.js
    """
    return send_from_directory(STATIC_DIR, filename)


# ── Health check (ALB target group uses this) ──────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":          "healthy",
        "service":         "dummy-infra-app",
        "active_errors":   len(_active_errors),
        "raw_logs_bucket": os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "last_ship":       shipper.last_s3_key or "never",
    }), 200


# ── Status ─────────────────────────────────────────────────────────────────────
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


# ── Trigger error ──────────────────────────────────────────────────────────────
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

    log_entry = simulator.generate_error(error_type)

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

    shipper.ship()

    return jsonify({
        "triggered":  error_type,
        "log_entry":  log_entry,
        "shipped_to": shipper.last_s3_key,
    }), 200


# ── Resolve error (called by SSL Lambda after remediation) ─────────────────────
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


# ── Log tail ───────────────────────────────────────────────────────────────────
@app.route("/api/dummy/logs", methods=["GET"])
def get_logs():
    try:
        n = min(max(int(request.args.get("lines", 30)), 1), 200)
        if not os.path.exists(LOG_FILE):
            return jsonify({"lines": ["(log file not yet created)"]})
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = [l.rstrip() for l in lines[-n:] if l.strip()]
        return jsonify({"lines": tail, "total_lines": len(lines)})
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": []}), 500


# ── Ship now ───────────────────────────────────────────────────────────────────
@app.route("/api/dummy/ship-now", methods=["POST"])
def ship_now():
    success = shipper.ship()
    if success:
        return jsonify({"shipped": True, "s3_key": shipper.last_s3_key}), 200
    return jsonify({"shipped": False,
                    "error": "Ship failed — check RAW_LOGS_BUCKET env var"}), 500


# ── SSL cert state ─────────────────────────────────────────────────────────────
@app.route("/api/dummy/ssl-cert", methods=["GET"])
def get_ssl_cert():
    with _state_lock:
        return jsonify(dict(_ssl_cert)), 200


# ── Helper ─────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", 5001))
    logger.info("dummy-infra-app starting on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
