# ──────────────────────────────────────────────────────────────────────────────
# lambda/lambda_handler.py  –  Log Processor Lambda
#
# Triggered by: S3 PUT event when app.py uploads a raw log to:
#   s3://<RAW_LOGS_BUCKET>/raw-logs/application.log
#
# What it does:
#   1. Downloads the raw log file from S3
#   2. Parses it using log_parser.py
#   3. Converts to CSV rows via log_to_csv_service.py
#   4. Writes processed CSV  → s3://<PROCESSED_BUCKET>/processed/csv/<stem>.csv
#   5. Writes unique errors  → s3://<PROCESSED_BUCKET>/processed/json/<stem>-errors.json
#   6. Also writes the fixed-name file the dashboard always reads:
#        s3://<PROCESSED_BUCKET>/processed/json/unique_errors.json
#
# Environment variables (set by CloudFormation):
#   RAW_LOGS_BUCKET  – source bucket (passed but not strictly needed; key from event)
#   PROCESSED_BUCKET – destination bucket for CSV + JSON output
#   PROCESSED_PREFIX – prefix inside processed bucket (default: processed/)
#   LOG_LEVEL        – logging verbosity (default: INFO)
# ──────────────────────────────────────────────────────────────────────────────

import json
import logging
import os
import tempfile
import urllib.parse
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# Modules from Conversion/ (co-packaged in the Lambda ZIP by CodeBuild)
from log_parser import parse_log_line           # type: ignore
from log_to_csv_service import (                # type: ignore
    convert_log_to_rows,
    write_rows_to_csv,
    write_unique_errors_json,
)

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("log-processor")

# ── AWS clients ───────────────────────────────────────────────────────────────
s3 = boto3.client("s3")

# ── Configuration ─────────────────────────────────────────────────────────────
PROCESSED_BUCKET  = os.environ.get("PROCESSED_BUCKET", "")
PROCESSED_PREFIX  = os.environ.get("PROCESSED_PREFIX", "processed/")

# Fixed S3 key the dashboard always reads for unique errors
FIXED_ERRORS_KEY  = f"{PROCESSED_PREFIX}json/unique_errors.json"


def handler(event, context):
    """
    Lambda entry point — triggered by S3 ObjectCreated events on raw-logs/ prefix.

    S3 event format:
    {
        "Records": [{
            "s3": {
                "bucket": {"name": "log-aggregator-raw-logs-..."},
                "object": {"key": "raw-logs/application.log"}
            }
        }]
    }
    """
    logger.info("Log processor Lambda invoked. RequestId: %s", context.aws_request_id)
    results = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key    = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        logger.info("Processing s3://%s/%s", bucket, key)

        try:
            result = _process_log_file(bucket, key)
            results.append({"key": key, "status": "success", **result})
        except Exception as exc:
            logger.error("Failed to process %s: %s", key, exc, exc_info=True)
            results.append({"key": key, "status": "error", "error": str(exc)})

    logger.info("Processing complete. Results: %s", json.dumps(results))
    return {"statusCode": 200, "body": json.dumps(results)}


def _process_log_file(bucket: str, key: str) -> dict:
    """
    Download a raw log file, convert it, and write outputs to S3.

    Output files written to PROCESSED_BUCKET:
      processed/csv/<stem>.csv          — full transaction CSV
      processed/json/<stem>-errors.json — unique errors for this file
      processed/json/unique_errors.json — fixed key always read by dashboard
    """
    output_bucket = PROCESSED_BUCKET or bucket
    stem = Path(key).stem  # e.g. "application" from "raw-logs/application.log"

    with tempfile.TemporaryDirectory() as tmpdir:
        # ── 1. Download raw log ───────────────────────────────────────────────
        local_log = os.path.join(tmpdir, "input.log")
        logger.debug("Downloading s3://%s/%s → %s", bucket, key, local_log)
        s3.download_file(bucket, key, local_log)

        # ── 2. Parse and convert ──────────────────────────────────────────────
        rows = convert_log_to_rows(local_log)

        # Support both old signature (returns list) and new (returns tuple)
        if isinstance(rows, tuple):
            rows, unique_errors_data = rows
        else:
            unique_errors_data = None

        logger.info("Parsed %d log rows", len(rows))

        # ── 3. Write to temp files ────────────────────────────────────────────
        csv_path    = os.path.join(tmpdir, f"{stem}.csv")
        errors_path = os.path.join(tmpdir, f"{stem}-errors.json")
        fixed_path  = os.path.join(tmpdir, "unique_errors.json")

        write_rows_to_csv(rows, csv_path)

        if unique_errors_data is not None:
            write_unique_errors_json(unique_errors_data, errors_path)
            write_unique_errors_json(unique_errors_data, fixed_path)
        else:
            write_unique_errors_json(rows, errors_path)
            write_unique_errors_json(rows, fixed_path)

        # ── 4. Upload processed files to S3 ──────────────────────────────────
        csv_s3_key    = f"{PROCESSED_PREFIX}csv/{stem}.csv"
        errors_s3_key = f"{PROCESSED_PREFIX}json/{stem}-errors.json"

        _upload(csv_path,    output_bucket, csv_s3_key,    "text/csv")
        _upload(errors_path, output_bucket, errors_s3_key, "application/json")
        # Always overwrite the fixed key so the dashboard gets latest data
        _upload(fixed_path,  output_bucket, FIXED_ERRORS_KEY, "application/json")

        logger.info(
            "Uploaded → csv: %s | errors: %s | fixed: %s",
            csv_s3_key, errors_s3_key, FIXED_ERRORS_KEY,
        )
        return {
            "rows":          len(rows),
            "csv_key":       csv_s3_key,
            "errors_key":    errors_s3_key,
            "fixed_key":     FIXED_ERRORS_KEY,
        }


def _upload(local_path: str, bucket: str, key: str, content_type: str = "application/octet-stream"):
    """Upload a local file to S3 with the given content type."""
    try:
        s3.upload_file(
            local_path, bucket, key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.debug("Uploaded s3://%s/%s", bucket, key)
    except ClientError as exc:
        logger.error("S3 upload failed for %s: %s", key, exc)
        raise
