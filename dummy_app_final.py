# dummy-infra-app/app.py
#
# CHANGES vs your version:
#
#  1. Valid error types come from ErrorSimulator.VALID_TYPES — no duplicate list.
#
#  2. Local directory layout is clearer.  When you run `python app.py` locally
#     the logs directory is created at:
#       <project>/dummy-infra-app/logs/
#         ├── app.log          — Flask request/response noise (health checks etc.)
#         ├── ssl_events.log   — ONLY triggered errors + resolutions (shipped to S3)
#         └── application.log  — Mirror copy of ssl_events.log (updated on every ship)
#                                This is the file that goes to S3 as
#                                raw-logs/application.log exactly.
#
#  3. The /api/dummy/debug endpoint also shows the local application.log path
#     so you can verify both files side-by-side.
#
#  4. The /api/dummy/logs endpoint now has a ?file= param:
#       ?file=events       → ssl_events.log  (default, what ships to S3)
#       ?file=application  → application.log (the S3 mirror copy)
#       ?file=app          → app.log         (Flask request noise)
#     This lets you verify each log independently without touching the container.
#
#  Everything else is identical to your version — all routes, state management,
#  background ship loop, and SSL cert tracking are unchanged.

import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, jsonify, request, send_from_directory

from error_simulator import ErrorSimulator
from log_shipper import LogShipper


# ══════════════════════════════════════════════════════════════════════════════
# LOG DIRECTORY — two log files + one mirror copy
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_log_dir() -> str:
    """
    Resolve the log directory with this priority:
      1. LOG_DIR env var — set in ECS task definition for container path
      2. /app/Dummy-infra-app/logs — standard container path (Dockerfile)
      3. <script_dir>/logs — local dev fallback, always works

    The chosen directory will contain:
      app.log          — Flask access log (never shipped)
      ssl_events.log   — Error/resolution events (shipped to S3)
      application.log  — Mirror of ssl_events.log (what actually goes to S3)
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

    # Local dev: creates  dummy-infra-app/logs/  next to this script
    local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(local_path, exist_ok=True)
    return local_path


LOG_DIR = _resolve_log_dir()

# ── File paths ────────────────────────────────────────────────────────────────
APP_LOG_FILE    = os.path.join(LOG_DIR, "app.log")          # Flask request noise
EVENTS_LOG_FILE = os.path.join(LOG_DIR, "ssl_events.log")   # Events shipped to S3
APP_MIRROR_FILE = os.path.join(LOG_DIR, "application.log")  # Mirror of what's in S3

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

# Suppress werkzeug health-check spam from app.log
logging.getLogger("werkzeug").setLevel(logging.ERROR)

logger.info("Log directory   : %s", LOG_DIR)
logger.info("app.log         : %s  (Flask request noise — NOT shipped)", APP_LOG_FILE)
logger.info("ssl_events.log  : %s  (errors + resolutions — shipped to S3)", EVENTS_LOG_FILE)
logger.info("application.log : %s  (mirror of S3 upload — verify locally)", APP_MIRROR_FILE)
logger.info("RAW_LOGS_BUCKET : %s", os.getenv("RAW_LOGS_BUCKET", "(not set — local dev)"))

# ── Flask app ─────────────────────────────────────────────────────────────────
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")

# ── Simulator and shipper ─────────────────────────────────────────────────────
simulator = ErrorSimulator(logger, EVENTS_LOG_FILE)
shipper   = LogShipper(logger, EVENTS_LOG_FILE)

# ── Shared state ──────────────────────────────────────────────────────────────
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

# ── Background ship loop — only fires if new events exist ─────────────────────
def _background_ship_loop():
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
    return jsonify({"status": "healthy", "service": "dummy-infra-app"}), 200


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


@app.route("/api/dummy/ssl-cert", methods=["GET"])
def get_ssl_cert():
    with _state_lock:
        return jsonify(dict(_ssl_cert)), 200


# ── Trigger ───────────────────────────────────────────────────────────────────

@app.route("/api/dummy/trigger-error", methods=["POST"])
def trigger_error():
    """
    Write a 3-line error cycle to ssl_events.log and ship immediately to S3.
    Body: {"error_type": "ssl_expired"}
    """
    body       = request.get_json(silent=True) or {}
    error_type = body.get("error_type", "").strip()

    if not error_type or error_type not in simulator.VALID_TYPES:
        return jsonify({
            "error":       f"Invalid or missing error_type.",
            "valid_types": simulator.VALID_TYPES,
        }), 400

    logger.info("Trigger: %s", error_type)

    try:
        log_entry = simulator.generate_error(error_type)
    except Exception as exc:
        logger.error("generate_error failed: %s", exc)
        return jsonify({
            "error":      f"Log write failed: {exc}",
            "events_log": EVENTS_LOG_FILE,
        }), 500

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

    ship_ok = shipper.ship()
    logger.info("Ship result: ok=%s key=%s", ship_ok, shipper.last_s3_key)

    return jsonify({
        "triggered":        error_type,
        "log_entry":        log_entry,
        "shipped":          ship_ok,
        "shipped_to":       shipper.last_s3_key or "(local only — RAW_LOGS_BUCKET not set)",
        # Local paths — open these to verify without needing S3 access
        "local_events_log":      EVENTS_LOG_FILE,
        "local_application_log": APP_MIRROR_FILE,
        "local_app_log":         APP_LOG_FILE,
    }), 200


# ── Resolve ───────────────────────────────────────────────────────────────────

@app.route("/api/dummy/resolve/<error_type>", methods=["POST"])
def resolve_error(error_type):
    """Called by Bedrock action group Lambdas after they fix an error."""
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
        "resolved":              error_type,
        "log_entry":             resolution_msg,
        "shipped":               ship_ok,
        "shipped_to":            shipper.last_s3_key or "(local only)",
        "local_events_log":      EVENTS_LOG_FILE,
        "local_application_log": APP_MIRROR_FILE,
    }), 200


# ── Log tail — supports ?file= param for all 3 log files ─────────────────────

@app.route("/api/dummy/logs", methods=["GET"])
def get_logs():
    """
    Returns the last N lines of a log file.

    Query params:
      ?lines=30                (default 30, max 200)
      ?file=events             ssl_events.log  — errors + resolutions (default)
      ?file=application        application.log — S3 mirror copy
      ?file=app                app.log         — Flask request noise

    This lets you verify each log independently from the browser or curl:
      curl http://localhost:5001/api/dummy/logs?file=events
      curl http://localhost:5001/api/dummy/logs?file=application
      curl http://localhost:5001/api/dummy/logs?file=app
    """
    file_param = request.args.get("file", "events").lower()
    file_map = {
        "events":      EVENTS_LOG_FILE,
        "application": APP_MIRROR_FILE,
        "app":         APP_LOG_FILE,
    }

    if file_param not in file_map:
        return jsonify({
            "error": f"Unknown file param '{file_param}'. Valid: {list(file_map.keys())}"
        }), 400

    target_file = file_map[file_param]

    try:
        n = min(max(int(request.args.get("lines", 30)), 1), 200)

        if not os.path.exists(target_file):
            return jsonify({
                "lines":       [f"({target_file} does not exist yet)"],
                "file":        target_file,
                "total_lines": 0,
            })

        with open(target_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        tail = [ln.rstrip() for ln in lines[-n:] if ln.strip()]
        return jsonify({
            "lines":       tail,
            "total_lines": len(lines),
            "file":        target_file,
        })
    except Exception as exc:
        return jsonify({"error": str(exc), "lines": []}), 500


# ── Force ship ────────────────────────────────────────────────────────────────

@app.route("/api/dummy/ship-now", methods=["POST"])
def ship_now():
    """Force-ship ssl_events.log to S3 right now (resets byte offset)."""
    original_pos = shipper._last_shipped_pos
    shipper._last_shipped_pos = 0
    success = shipper.ship()
    if not success:
        shipper._last_shipped_pos = original_pos

    if success:
        return jsonify({
            "shipped":               True,
            "s3_key":                shipper.last_s3_key,
            "local_events_log":      EVENTS_LOG_FILE,
            "local_application_log": APP_MIRROR_FILE,
        }), 200

    return jsonify({
        "shipped":               False,
        "error":                 "Ship failed — check RAW_LOGS_BUCKET and log file",
        "bucket":                os.getenv("RAW_LOGS_BUCKET", "(not set)"),
        "events_log_exists":     os.path.exists(EVENTS_LOG_FILE),
        "local_events_log":      EVENTS_LOG_FILE,
        "local_application_log": APP_MIRROR_FILE,
    }), 500


# ── Debug ─────────────────────────────────────────────────────────────────────

@app.route("/api/dummy/debug", methods=["GET"])
def debug():
    """
    Verify log paths, file sizes, permissions, and S3 config.
    Call this first when troubleshooting.
    """
    def _check(path):
        exists = os.path.exists(path)
        size   = os.path.getsize(path) if exists else 0
        try:
            with open(path, "a"):
                pass
            writable = True
        except Exception:
            writable = False
        return {
            "path":     path,
            "exists":   exists,
            "bytes":    size,
            "writable": writable,
        }

    return jsonify({
        "log_dir":               LOG_DIR,
        "files": {
            "app_log":          _check(APP_LOG_FILE),
            "ssl_events_log":   _check(EVENTS_LOG_FILE),
            "application_log":  _check(APP_MIRROR_FILE),   # S3 mirror
        },
        "shipper": {
            "shipped_up_to_bytes": shipper._last_shipped_pos,
            "has_new_events":      shipper.has_new_events(),
            "last_s3_key":         shipper.last_s3_key or "never",
        },
        "env": {
            "raw_logs_bucket":  os.getenv("RAW_LOGS_BUCKET",     "(not set)"),
            "raw_logs_prefix":  os.getenv("RAW_LOGS_PREFIX",     "raw-logs/"),
            "aws_region":       os.getenv("AWS_DEFAULT_REGION",  "us-east-1"),
        },
        "active_errors": list(_active_errors.keys()),
    }), 200


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("APP_PORT", 5001))
    logger.info("Starting dummy-infra-app on port %d", port)
    logger.info("")
    logger.info("Local log files (open to verify without S3):")
    logger.info("  Events (shipped to S3): %s", EVENTS_LOG_FILE)
    logger.info("  S3 mirror copy:         %s", APP_MIRROR_FILE)
    logger.info("  Flask request log:      %s", APP_LOG_FILE)
    logger.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
