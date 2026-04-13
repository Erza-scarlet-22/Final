# Dashboard data service for the Log Aggregator.
#
# ── What changed vs original (AWS only) ──────────────────────────────────────
#   When PROCESSED_BUCKET env var is set (running in ECS/AWS), this service
#   reads unique_errors.json and converted_application_logs.csv directly from
#   S3 instead of local Conversion/ files.
#
#   Locally (PROCESSED_BUCKET not set): 100% identical behaviour to original.
#
# ── Response structure (unchanged — must match dashboard.html exactly) ────────
#   {
#     "summary": {
#       "uniqueErrorTypes": int,
#       "totalErrorEvents": int,
#       "statusCodeCount":  int,
#       "apiCount":         int
#     },
#     "byStatus": { "401": 22, "503": 8, ... },
#     "byApi":    { "POST /api/auth/token": 22, ... },
#     "rows":     [ { "Status Code", "Error Code", "Description",
#                     "API", "Count", "Last Seen", "Dates" }, ... ],
#     "filter":   { "from": null, "to": null, "label": "All Time" }
#   }
#
# ── S3 key structure written by lambda_handler.py ─────────────────────────────
#   processed/csv/application.csv          ← all transaction rows
#   processed/json/unique_errors.json      ← pre-aggregated errors (fixed key)
#   processed/json/application-errors.json ← same, per-file name

import csv
import io
import json
import logging
import os
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)

# ── Shared field-name constants ───────────────────────────────────────────────
# These must match what write_unique_errors_json() puts in the JSON.
STATUS_CODE_KEY = 'Status Code'    # Capital C — matches unique_errors.json output
ERROR_CODE_KEY  = 'Error Code'
DESCRIPTION_KEY = 'Description'
API_KEY         = 'API'
COUNT_KEY       = 'Count'
LAST_SEEN_KEY   = 'Last Seen'
UNIQUE_ERRORS_JSON_FILENAME        = 'unique_errors.json'
LEGACY_UNIQUE_ERRORS_JSON_FILENAME = 'unique errors.json'


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _get_s3_config():
    """
    Return (s3_client, bucket, prefix) at call time — not at import time.
    This avoids the bug where PROCESSED_BUCKET env var isn't set yet when
    the module is first imported.
    """
    bucket = os.getenv('PROCESSED_BUCKET', '').strip()
    prefix = os.getenv('PROCESSED_LOG_PREFIX', 'processed/')
    region = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

    if not bucket:
        return None, '', prefix

    try:
        import boto3
        s3 = boto3.client('s3', region_name=region)
        return s3, bucket, prefix
    except Exception as exc:
        _logger.error("boto3 unavailable: %s", exc)
        return None, '', prefix


def _s3_read_json(s3, bucket: str, key: str) -> Optional[list]:
    """Download and JSON-parse a file from S3. Returns None on any error."""
    try:
        obj  = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(obj['Body'].read().decode('utf-8'))
        _logger.info("S3 JSON read OK: s3://%s/%s (%d entries)", bucket, key, len(data) if isinstance(data, list) else -1)
        return data if isinstance(data, list) else []
    except Exception as exc:
        code = getattr(getattr(exc, 'response', None), 'get', lambda k, d=None: d)('Error', {}).get('Code', '')
        if code == 'NoSuchKey':
            _logger.warning("S3 key not found: s3://%s/%s", bucket, key)
        else:
            _logger.error("S3 read error s3://%s/%s: %s", bucket, key, exc)
        return None


def _s3_read_csv(s3, bucket: str, key: str) -> List[Dict]:
    """Download and CSV-parse a file from S3. Returns [] on any error."""
    try:
        obj     = s3.get_object(Bucket=bucket, Key=key)
        content = obj['Body'].read().decode('utf-8')
        reader  = csv.DictReader(io.StringIO(content))
        rows    = list(reader)
        _logger.info("S3 CSV read OK: s3://%s/%s (%d rows)", bucket, key, len(rows))
        return rows
    except Exception as exc:
        code = getattr(getattr(exc, 'response', None), 'get', lambda k, d=None: d)('Error', {}).get('Code', '')
        if code == 'NoSuchKey':
            _logger.warning("S3 CSV key not found: s3://%s/%s", bucket, key)
        else:
            _logger.error("S3 CSV read error s3://%s/%s: %s", bucket, key, exc)
        return []


# ── Data readers ──────────────────────────────────────────────────────────────

def _read_unique_errors_data(conversion_dir: str) -> List[dict]:
    """
    Load pre-aggregated unique-errors list.

    In AWS  (PROCESSED_BUCKET set): reads from S3 processed/json/unique_errors.json
    Locally (PROCESSED_BUCKET empty): reads from local Conversion/ directory
    """
    s3, bucket, prefix = _get_s3_config()

    # ── AWS path ──────────────────────────────────────────────────────────────
    if s3 and bucket:
        fixed_key = f"{prefix}json/{UNIQUE_ERRORS_JSON_FILENAME}"
        data = _s3_read_json(s3, bucket, fixed_key)
        if data is not None:
            return data

        _logger.warning(
            "unique_errors.json not found in S3 at s3://%s/%s — "
            "Lambda may not have run yet. "
            "Trigger /api/simulate-traffic then wait ~10 seconds.",
            bucket, fixed_key,
        )
        return []

    # ── Local path (identical to original) ───────────────────────────────────
    for filename in (UNIQUE_ERRORS_JSON_FILENAME, LEGACY_UNIQUE_ERRORS_JSON_FILENAME):
        path = os.path.join(conversion_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            continue
    return []


def _read_csv_for_date_filter(conversion_dir: str) -> List[Dict]:
    """
    Load all CSV rows for date-filtered queries.

    In AWS: reads from S3 processed/csv/application.csv
    Locally: reads from local Conversion/ directory
    """
    s3, bucket, prefix = _get_s3_config()

    # ── AWS path ──────────────────────────────────────────────────────────────
    if s3 and bucket:
        csv_key = f"{prefix}csv/application.csv"
        rows = _s3_read_csv(s3, bucket, csv_key)
        if rows:
            return rows
        # Try listing for any CSV if fixed name not found
        try:
            resp = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}csv/")
            keys = sorted(
                [o['Key'] for o in resp.get('Contents', []) if o['Key'].endswith('.csv')],
                reverse=True,
            )
            if keys:
                return _s3_read_csv(s3, bucket, keys[0])
        except Exception as exc:
            _logger.error("S3 list error: %s", exc)
        return []

    # ── Local path (identical to original) ───────────────────────────────────
    csv_path = os.path.join(conversion_dir, 'converted_application_logs.csv')
    if not os.path.exists(csv_path):
        return []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        _logger.error("Local CSV read error: %s", exc)
        return []


# ── Date filter helpers (identical to original) ───────────────────────────────

def _resolve_date_filters(
    request_args: dict,
) -> Tuple[Optional[date], Optional[date], str]:
    date_from_str = request_args.get('from')
    date_to_str   = request_args.get('to')
    preset        = request_args.get('preset')
    today         = date.today()

    if preset == 'today':
        return today, today, 'Today'
    if preset == 'week':
        return today - timedelta(days=6), today, 'Last 7 Days'
    if preset == 'month':
        return today - timedelta(days=29), today, 'Last 30 Days'
    if preset == 'quarter':
        return today - timedelta(days=89), today, 'Last 90 Days'

    try:
        date_from = date.fromisoformat(date_from_str) if date_from_str else None
        date_to   = date.fromisoformat(date_to_str)   if date_to_str   else None
    except ValueError:
        date_from = None
        date_to   = None

    if date_from or date_to:
        from_label = date_from.isoformat() if date_from else '...'
        to_label   = date_to.isoformat()   if date_to   else '...'
        return date_from, date_to, f'{from_label} to {to_label}'

    return None, None, 'All Time'


def _row_is_in_range(row_date: date, date_from: Optional[date], date_to: Optional[date]) -> bool:
    if date_from and row_date < date_from:
        return False
    if date_to and row_date > date_to:
        return False
    return True


def _update_aggregated_error(
    aggregated: dict, row: dict, row_date_str: str, row_timestamp_str: str
) -> None:
    """
    Upsert a CSV row into the in-memory aggregation dict.
    NOTE: CSV uses 'Status code' (lowercase c).
          JSON output and dashboard use 'Status Code' (uppercase C).
          We normalise to uppercase here.
    """
    status_code = row.get('Status code', row.get('Status Code', ''))
    error_code  = row.get(ERROR_CODE_KEY, '')
    description = row.get(DESCRIPTION_KEY, '')
    api         = row.get(API_KEY, '')

    # Only aggregate error rows (status >= 400 with an error code)
    if not status_code.isdigit() or int(status_code) < 400:
        return
    if not error_code:
        return

    key = (status_code, error_code, description, api)
    if key not in aggregated:
        aggregated[key] = {'count': 0, 'dates': set(), 'last_seen': ''}
    aggregated[key]['count'] += 1
    if row_date_str:
        aggregated[key]['dates'].add(row_date_str)
    if row_timestamp_str and row_timestamp_str > aggregated[key]['last_seen']:
        aggregated[key]['last_seen'] = row_timestamp_str
    elif row_date_str and row_date_str > aggregated[key]['last_seen']:
        aggregated[key]['last_seen'] = row_date_str


def _serialize_aggregated_errors(aggregated: dict) -> List[dict]:
    """Convert aggregation dict to sorted list of dashboard row dicts."""
    return [
        {
            STATUS_CODE_KEY: key[0],
            ERROR_CODE_KEY:  key[1],
            DESCRIPTION_KEY: key[2],
            API_KEY:         key[3],
            COUNT_KEY:       meta['count'],
            LAST_SEEN_KEY:   meta['last_seen'],
            'Dates':         sorted(meta['dates']),
        }
        for key, meta in sorted(aggregated.items())
    ]


def _collect_unique_errors(
    conversion_dir: str,
    date_from: Optional[date],
    date_to: Optional[date],
) -> List[dict]:
    """
    Return unique error records for the requested date range.

    No filter active → return pre-built JSON (fastest path).
    Filter active    → re-aggregate from CSV rows.
    """
    if not (date_from or date_to):
        return _read_unique_errors_data(conversion_dir)

    # Date filter active — re-aggregate from CSV
    all_rows   = _read_csv_for_date_filter(conversion_dir)
    aggregated: dict = {}

    for row in all_rows:
        row_date_str      = row.get('Date', '')
        row_timestamp_str = row.get('Timestamp', '')
        try:
            row_date = date.fromisoformat(row_date_str)
        except ValueError:
            continue
        if not _row_is_in_range(row_date, date_from, date_to):
            continue
        _update_aggregated_error(aggregated, row, row_date_str, row_timestamp_str)

    return _serialize_aggregated_errors(aggregated)


# ── Public API ────────────────────────────────────────────────────────────────

def build_dashboard_payload(
    conversion_dir: str,
    run_conversion_outputs,
    request_args: dict,
) -> dict:
    """
    Assemble the full dashboard payload dict.

    In AWS:  reads from S3 (PROCESSED_BUCKET env var set by CloudFormation).
    Locally: triggers local conversion then reads from Conversion/ directory.

    Response structure (must match dashboard.html exactly):
    {
      "summary": { uniqueErrorTypes, totalErrorEvents, statusCodeCount, apiCount },
      "byStatus": { "401": 22, ... },
      "byApi":    { "POST /api/auth/token": 22, ... },
      "rows":     [ { Status Code, Error Code, Description, API, Count,
                      Last Seen, Dates }, ... ],
      "filter":   { from, to, label }
    }
    """
    s3, bucket, _ = _get_s3_config()
    running_in_aws = bool(s3 and bucket)

    # In AWS: Lambda already processed the file — just read from S3.
    # Locally: regenerate artefacts from the latest log first.
    if not running_in_aws:
        run_conversion_outputs()

    date_from, date_to, filter_label = _resolve_date_filters(request_args)
    unique_errors = _collect_unique_errors(conversion_dir, date_from, date_to)

    total_errors = sum(int(item.get(COUNT_KEY, 0)) for item in unique_errors)

    by_status: Dict[str, int] = {}
    by_api:    Dict[str, int] = {}
    for item in unique_errors:
        status = str(item.get(STATUS_CODE_KEY, 'Unknown'))
        api    = str(item.get(API_KEY,          'Unknown'))
        count  = int(item.get(COUNT_KEY, 0))
        by_status[status] = by_status.get(status, 0) + count
        by_api[api]       = by_api.get(api,    0) + count

    return {
        'summary': {
            'uniqueErrorTypes': len(unique_errors),
            'totalErrorEvents': total_errors,
            'statusCodeCount':  len(by_status),
            'apiCount':         len(by_api),
        },
        'byStatus': by_status,
        'byApi':    by_api,
        'rows':     unique_errors,
        'filter': {
            'from':  date_from.isoformat() if date_from else None,
            'to':    date_to.isoformat()   if date_to   else None,
            'label': filter_label,
        },
    }
