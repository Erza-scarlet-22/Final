# ══════════════════════════════════════════════════════════════════════════════
# lambda/lambda_handler.py  –  Log Processor Lambda (entry point)
#
# ── What is a Lambda Handler? ─────────────────────────────────────────────────
# AWS Lambda needs one Python function it can call when an event fires.
# That function is called the "handler". In CloudFormation we set:
#     Handler: lambda_handler.handler
# which means: in the file lambda_handler.py, call the function named handler().
#
# ── How this file connects all the conversion modules ────────────────────────
#
#  S3 event fires
#       │
#       ▼
#  handler(event, context)          ← THIS FILE — entry point only
#       │  reads bucket + key from S3 event
#       │
#       ▼
#  _process_log_file(bucket, key)   ← THIS FILE — orchestration
#       │
#       ├─ boto3 s3.download_file() → downloads raw .log to /tmp
#       │
#       ├─ log_to_csv_service.convert_log_to_rows()
#       │       │  internally imports log_parser.py for regex patterns
#       │       │  reads the raw log line by line
#       │       └→ returns List[Dict] — one dict per API request cycle
#       │
#       ├─ log_to_csv_service.write_rows_to_csv()
#       │       └→ writes converted_application_logs CSV to /tmp
#       │
#       ├─ log_to_csv_service.write_unique_errors_json()
#       │       └→ aggregates errors, writes unique_errors.json to /tmp
#       │
#       └─ boto3 s3.upload_file() × 3
#               ├→ s3://<processed>/processed/csv/<stem>.csv
#               ├→ s3://<processed>/processed/json/<stem>-errors.json
#               └→ s3://<processed>/processed/json/unique_errors.json
#                        (fixed key — dashboard always reads this one)
#
# ── What log_parser.py does ───────────────────────────────────────────────────
# log_parser.py is a LOW-LEVEL utility module. It only contains:
#   - Compiled regex patterns (API_WITH_IP_PATTERN, STATUS_PATTERN, etc.)
#   - Helper functions: clean_line(), extract_error_details(),
#                       extract_timestamp(), extract_date()
# It does NOT have a parse_log_line() function.
# We never import log_parser directly in this file — log_to_csv_service
# handles all the parsing internally using log_parser.
#
# ── Environment variables (set by CloudFormation) ─────────────────────────────
#   PROCESSED_BUCKET  — destination S3 bucket for CSV + JSON output
#   PROCESSED_PREFIX  — prefix inside processed bucket (default: processed/)
#   LOG_LEVEL         — logging verbosity (default: INFO)
# ══════════════════════════════════════════════════════════════════════════════

import json
import logging
import os
import tempfile
import urllib.parse
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ── Conversion module imports ─────────────────────────────────────────────────
# These files are co-packaged inside the Lambda ZIP by CodeBuild.
# CodeBuild copies Conversion/log_parser.py and Conversion/log_to_csv_service.py
# into the same flat directory as this file, so they import cleanly.
#
# IMPORTANT: Do NOT import parse_log_line from log_parser — it does not exist.
# log_parser only exports: clean_line, extract_error_details,
#                          extract_timestamp, extract_date
# log_to_csv_service is the correct public API for all conversion work.
from log_to_csv_service import (   # type: ignore
    convert_log_to_rows,
    write_rows_to_csv,
    write_unique_errors_json,
)

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("log-processor")

# ── AWS clients ───────────────────────────────────────────────────────────────
# Initialised once per Lambda container (warm starts reuse these).
s3 = boto3.client("s3")

# ── Configuration from environment ───────────────────────────────────────────
PROCESSED_BUCKET = os.environ.get("PROCESSED_BUCKET", "")
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", "processed/")

# This fixed S3 key is always overwritten on every run.
# The dashboard always reads from this exact path.
FIXED_ERRORS_KEY = f"{PROCESSED_PREFIX}json/unique_errors.json"


# ══════════════════════════════════════════════════════════════════════════════
# HANDLER — AWS calls this function when an S3 event fires
# ══════════════════════════════════════════════════════════════════════════════

def handler(event, context):
    """
    Lambda entry point. AWS invokes this function automatically when
    a file is uploaded to s3://<raw-bucket>/raw-logs/

    The S3 event looks like this:
    {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "log-aggregator-raw-logs-123456789"},
                    "object": {"key": "raw-logs/application.log"}
                }
            }
        ]
    }

    Returns: {"statusCode": 200, "body": "[{...results...}]"}
    """
    logger.info("=== Log Processor Lambda invoked ===")
    logger.info("RequestId: %s", context.aws_request_id)
    logger.info("Event: %s", json.dumps(event))
    logger.info("PROCESSED_BUCKET = %s", PROCESSED_BUCKET or "(not set — will use source bucket)")
    logger.info("PROCESSED_PREFIX  = %s", PROCESSED_PREFIX)

    results = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        # URL-decode the key (S3 encodes spaces and special chars)
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        logger.info("--- Processing s3://%s/%s ---", bucket, key)

        try:
            result = _process_log_file(bucket, key)
            results.append({"key": key, "status": "success", **result})
            logger.info(
                "SUCCESS: %s — rows=%d csv=%s errors=%s",
                key, result["rows"], result["csv_key"], result["errors_key"],
            )
        except Exception as exc:
            logger.error("FAILED: %s — %s", key, exc, exc_info=True)
            results.append({"key": key, "status": "error", "error": str(exc)})

    logger.info("=== Lambda complete. Results: %s ===", json.dumps(results))
    return {"statusCode": 200, "body": json.dumps(results)}


# ══════════════════════════════════════════════════════════════════════════════
# CORE PROCESSING — orchestrates all conversion modules
# ══════════════════════════════════════════════════════════════════════════════

def _process_log_file(bucket: str, key: str) -> dict:
    """
    Full pipeline for one raw log file:

    Step 1 — Download raw .log from S3 raw-logs bucket → /tmp/input.log
    Step 2 — Convert using log_to_csv_service:
                convert_log_to_rows()        reads the log line by line
                                             uses log_parser internally for regex
                                             returns List[Dict] of API transactions
    Step 3 — Write outputs to /tmp:
                write_rows_to_csv()          → /tmp/<stem>.csv
                write_unique_errors_json()   → /tmp/<stem>-errors.json
                                             → /tmp/unique_errors.json (fixed key)
    Step 4 — Upload all 3 files to processed S3 bucket

    Returns dict with row count and S3 keys of uploaded files.
    """
    output_bucket = PROCESSED_BUCKET or bucket
    stem = Path(key).stem  # "application" from "raw-logs/application.log"

    with tempfile.TemporaryDirectory() as tmpdir:

        # ── Step 1: Download raw log from S3 ─────────────────────────────────
        local_log = os.path.join(tmpdir, "input.log")
        logger.info("Step 1: Downloading s3://%s/%s", bucket, key)
        s3.download_file(bucket, key, local_log)
        file_size = os.path.getsize(local_log)
        logger.info("Downloaded %d bytes to %s", file_size, local_log)

        if file_size == 0:
            raise ValueError(f"Downloaded log file is empty: s3://{bucket}/{key}")

        # ── Step 2: Convert log to rows ───────────────────────────────────────
        # convert_log_to_rows() internally uses log_parser.py regex patterns
        # to parse 3-line request cycles from the Flask application log.
        # Returns List[Dict[str, str]] — one dict per completed API request.
        logger.info("Step 2: Converting log to rows using log_to_csv_service")
        rows = convert_log_to_rows(local_log)
        logger.info("Parsed %d transaction rows from log", len(rows))

        if len(rows) == 0:
            logger.warning(
                "No rows parsed from %s — log may not contain completed "
                "request cycles yet. Dashboard will remain empty until "
                "traffic flows through the app.", key
            )

        # ── Step 3: Write output files to /tmp ───────────────────────────────
        csv_path    = os.path.join(tmpdir, f"{stem}.csv")
        errors_path = os.path.join(tmpdir, f"{stem}-errors.json")
        fixed_path  = os.path.join(tmpdir, "unique_errors.json")

        logger.info("Step 3a: Writing CSV → %s", csv_path)
        write_rows_to_csv(rows, csv_path)

        logger.info("Step 3b: Writing errors JSON → %s", errors_path)
        write_unique_errors_json(rows, errors_path)

        # Fixed key — same content, but always the same filename.
        # The dashboard always reads processed/json/unique_errors.json
        logger.info("Step 3c: Writing fixed-key errors JSON → %s", fixed_path)
        write_unique_errors_json(rows, fixed_path)

        # ── Step 4: Upload all 3 files to processed S3 bucket ────────────────
        csv_key    = f"{PROCESSED_PREFIX}csv/{stem}.csv"
        errors_key = f"{PROCESSED_PREFIX}json/{stem}-errors.json"

        logger.info("Step 4: Uploading to s3://%s/", output_bucket)
        _s3_upload(csv_path,    output_bucket, csv_key,          "text/csv")
        _s3_upload(errors_path, output_bucket, errors_key,       "application/json")
        _s3_upload(fixed_path,  output_bucket, FIXED_ERRORS_KEY, "application/json")

        logger.info(
            "All uploads complete:\n  csv    → %s\n  errors → %s\n  fixed  → %s",
            csv_key, errors_key, FIXED_ERRORS_KEY,
        )

        return {
            "rows":       len(rows),
            "csv_key":    csv_key,
            "errors_key": errors_key,
            "fixed_key":  FIXED_ERRORS_KEY,
        }


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _s3_upload(local_path: str, bucket: str, key: str, content_type: str):
    """Upload a single local file to S3. Raises ClientError on failure."""
    try:
        s3.upload_file(
            local_path, bucket, key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.debug("Uploaded s3://%s/%s (%s)", bucket, key, content_type)
    except ClientError as exc:
        logger.error(
            "S3 upload FAILED for s3://%s/%s: %s", bucket, key, exc
        )
        raise
