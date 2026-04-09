# Application entry point for the Log Aggregator API.
#
# AWS Changes vs local:
#   - logger.py writes to /app/Application/logs/application.log (file in container)
#   - After every request, _schedule_s3_upload() uploads the log file to
#     s3://$RAW_LOGS_BUCKET/raw-logs/application.log
#   - This triggers the Lambda processor automatically via S3 event
#   - Dashboard reads from PROCESSED_BUCKET instead of local Conversion/ dir

from flask import Flask, jsonify, request
from logger import info, error, warn
import os
import sys
import time
import threading
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# ── Directory resolution ──────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
CONVERSION_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'Conversion'))
DASHBOARD_DIR  = os.path.abspath(os.path.join(BASE_DIR, '..', 'Dashboard'))

load_dotenv(os.path.abspath(os.path.join(BASE_DIR, '..', '.env')), override=True)

# ── Application Configuration ─────────────────────────────────────────────────
APP_PORT         = int(os.getenv('APP_PORT', '5000'))
APP_HOST         = os.getenv('APP_HOST', 'localhost')
FLASK_DEBUG      = os.getenv('FLASK_DEBUG', 'true').lower() in ('true', '1', 'yes')
APP_LOG_FILENAME = os.getenv('LOG_FILENAME', 'application.log')

# ── AWS S3 Configuration ──────────────────────────────────────────────────────
AWS_REGION      = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
RAW_LOGS_BUCKET = os.getenv('RAW_LOGS_BUCKET', '')
RAW_LOGS_PREFIX = os.getenv('RAW_LOGS_PREFIX', 'raw-logs/')

# S3 upload is only active when RAW_LOGS_BUCKET is set (i.e. running in AWS).
# Locally this stays empty so behaviour is identical to before.
S3_UPLOAD_ENABLED = bool(RAW_LOGS_BUCKET)

# S3 client — only initialised when upload is enabled.
_s3_client = boto3.client('s3', region_name=AWS_REGION) if S3_UPLOAD_ENABLED else None

# Add sibling directories to sys.path
for module_dir in (CONVERSION_DIR, DASHBOARD_DIR):
    if module_dir not in sys.path:
        sys.path.append(module_dir)

# ── Optional dependency flags ─────────────────────────────────────────────────
try:
    from log_to_csv_service import convert_log_to_rows, write_rows_to_csv, write_unique_errors_json
    CONVERTER_AVAILABLE = True
except Exception:
    CONVERTER_AVAILABLE = False

try:
    from dashboard_blueprint import create_dashboard_blueprint
    DASHBOARD_AVAILABLE = True
except Exception:
    DASHBOARD_AVAILABLE = False

# ── Flask application ─────────────────────────────────────────────────────────
app = Flask(__name__)

# Debounce timers — prevent bursts of requests from hammering S3/conversion.
_conversion_timer: threading.Timer | None = None
_s3_upload_timer:  threading.Timer | None = None
_conversion_lock  = threading.Lock()
_s3_lock          = threading.Lock()
_CONVERSION_DEBOUNCE_SECONDS = 2.0
_S3_UPLOAD_DEBOUNCE_SECONDS  = 5.0   # Upload at most once every 5 s


# ── S3 upload helper ──────────────────────────────────────────────────────────

def _do_s3_upload():
    """
    Upload the current application.log to S3 raw-logs bucket.
    This triggers the Lambda processor via S3 event notification.

    Key written: raw-logs/application.log
    (Overwriting the same key on each upload is intentional — the Lambda
    processes whatever is there; the S3 event fires on every PUT.)
    """
    if not S3_UPLOAD_ENABLED or _s3_client is None:
        return

    source_log = os.path.join(BASE_DIR, 'logs', APP_LOG_FILENAME)
    if not os.path.exists(source_log):
        warn("S3 upload skipped — log file does not exist yet")
        return

    s3_key = f"{RAW_LOGS_PREFIX}{APP_LOG_FILENAME}"
    try:
        _s3_client.upload_file(source_log, RAW_LOGS_BUCKET, s3_key)
        info(f"Log uploaded to s3://{RAW_LOGS_BUCKET}/{s3_key}")
    except ClientError as exc:
        error(f"S3 upload failed: {exc}")
    except Exception as exc:
        error(f"S3 upload unexpected error: {exc}")


def _schedule_s3_upload():
    """Debounced S3 upload — resets the timer on every call within the window."""
    if not S3_UPLOAD_ENABLED:
        return
    global _s3_upload_timer
    with _s3_lock:
        if _s3_upload_timer is not None:
            _s3_upload_timer.cancel()
        _s3_upload_timer = threading.Timer(_S3_UPLOAD_DEBOUNCE_SECONDS, _do_s3_upload)
        _s3_upload_timer.daemon = True
        _s3_upload_timer.start()


# ── Local conversion helper (unchanged from original) ────────────────────────

def _schedule_conversion():
    """Schedule a single deferred local conversion run."""
    global _conversion_timer
    with _conversion_lock:
        if _conversion_timer is not None:
            _conversion_timer.cancel()
        _conversion_timer = threading.Timer(_CONVERSION_DEBOUNCE_SECONDS, run_conversion_outputs)
        _conversion_timer.daemon = True
        _conversion_timer.start()


def run_conversion_outputs():
    """
    Parse the latest application log and regenerate CSV and unique-errors JSON.
    Used locally (no S3) and also called after simulate-traffic to update the
    local Conversion/ directory for the dashboard.
    """
    if not CONVERTER_AVAILABLE:
        return

    source_log                = os.path.join(BASE_DIR, 'logs', APP_LOG_FILENAME)
    output_csv                = os.path.join(CONVERSION_DIR, 'converted_application_logs.csv')
    output_unique_errors_json = os.path.join(CONVERSION_DIR, 'unique_errors.json')

    try:
        rows = convert_log_to_rows(source_log)
        write_rows_to_csv(rows, output_csv)
        write_unique_errors_json(rows, output_unique_errors_json)
    except Exception as conversion_error:
        warn(f"Log conversion failed: {str(conversion_error)}")

    # Also schedule an S3 upload so the Lambda gets the freshest file.
    _schedule_s3_upload()


# ── Blueprint registration ─────────────────────────────────────────────────────
from routes.core           import core_bp
from routes.payments       import payments_bp
from routes.auth           import auth_bp
from routes.orders         import orders_bp
from routes.users          import users_bp
from routes.infrastructure import infrastructure_bp
from routes.simulator      import create_simulator_blueprint

app.register_blueprint(core_bp)
app.register_blueprint(payments_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(orders_bp)
app.register_blueprint(users_bp)
app.register_blueprint(infrastructure_bp)
app.register_blueprint(create_simulator_blueprint(BASE_DIR, APP_LOG_FILENAME, run_conversion_outputs))

if DASHBOARD_AVAILABLE:
    app.register_blueprint(create_dashboard_blueprint(CONVERSION_DIR, run_conversion_outputs))


# ── Middleware ─────────────────────────────────────────────────────────────────

@app.before_request
def log_request():
    info(f"{request.method} {request.path}", f"IP: {request.remote_addr}")


@app.after_request
def log_response(response):
    info(f"{request.method} {request.path}", f"Status Code: {response.status_code}")
    # Run local conversion AND schedule S3 upload after every request.
    _schedule_conversion()
    _schedule_s3_upload()
    return response


# ── HTTP error handlers ────────────────────────────────────────────────────────

@app.errorhandler(400)
def bad_request(exc):
    error(f"Bad request: {str(exc)}", {"error_code": 4000})
    return jsonify({"error": "Bad request", "error_code": 4000}), 400

@app.errorhandler(401)
def unauthorized(exc):
    error("Unauthorized access attempt", {"error_code": 4001})
    return jsonify({"error": "Unauthorized", "error_code": 4001}), 401

@app.errorhandler(403)
def forbidden(exc):
    error(f"Forbidden access: {str(exc)}", {"error_code": 4003})
    return jsonify({"error": "Forbidden", "error_code": 4003}), 403

@app.errorhandler(404)
def not_found(exc):
    warn(f"Endpoint not found: {request.method} {request.path}", {"error_code": 4004})
    return jsonify({"error": "Endpoint not found", "error_code": 4004}), 404

@app.errorhandler(500)
def internal_error(exc):
    error(f"Internal server error: {str(exc)}", {"error_code": 5000})
    return jsonify({"error": "Internal server error", "error_code": 5000}), 500


# ── Dev server entry point ─────────────────────────────────────────────────────

if __name__ == '__main__':
    run_conversion_outputs()
    print(f"\nAPI running at http://{APP_HOST}:{APP_PORT}")
    print(f"S3 upload enabled: {S3_UPLOAD_ENABLED}")
    if S3_UPLOAD_ENABLED:
        print(f"  → Bucket : {RAW_LOGS_BUCKET}")
        print(f"  → Prefix : {RAW_LOGS_PREFIX}")
    app.run(host=APP_HOST, port=APP_PORT, debug=FLASK_DEBUG)
