# Dashboard data service for the Log Aggregator.
#
# AWS Changes:
#   When PROCESSED_BUCKET env var is set (running in ECS), this service reads
#   the processed unique_errors.json and CSV directly from S3 instead of
#   local Conversion/ files. Locally the behaviour is unchanged.

import csv
import json
import logging
import os
import tempfile
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

_logger = logging.getLogger(__name__)

# ── Shared field-name constants ───────────────────────────────────────────────
STATUS_CODE_KEY = 'Status Code'
ERROR_CODE_KEY  = 'Error Code'
DESCRIPTION_KEY = 'Description'
API_KEY         = 'API'
COUNT_KEY       = 'Count'
LAST_SEEN_KEY   = 'Last Seen'
UNIQUE_ERRORS_JSON_FILENAME        = 'unique_errors.json'
LEGACY_UNIQUE_ERRORS_JSON_FILENAME = 'unique errors.json'

# ── AWS S3 configuration ──────────────────────────────────────────────────────
_PROCESSED_BUCKET = os.getenv('PROCESSED_BUCKET', '')
_PROCESSED_PREFIX = os.getenv('PROCESSED_LOG_PREFIX', 'processed/')
_AWS_REGION       = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')
_S3_ENABLED       = bool(_PROCESSED_BUCKET)
_s3               = boto3.client('s3', region_name=_AWS_REGION) if _S3_ENABLED else None


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3_get_json(key: str) -> Optional[list]:
    """Download and parse a JSON file from the processed S3 bucket."""
    try:
        obj = _s3.get_object(Bucket=_PROCESSED_BUCKET, Key=key)
        data = json.loads(obj['Body'].read().decode('utf-8'))
        return data if isinstance(data, list) else []
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'NoSuchKey':
            _logger.warning("S3 key not found: s3://%s/%s", _PROCESSED_BUCKET, key)
        else:
            _logger.error("S3 error reading %s: %s", key, exc)
    except Exception as exc:
        _logger.error("Error reading S3 JSON %s: %s", key, exc)
    return None


def _s3_get_csv_rows(key: str) -> List[Dict]:
    """Download and parse a CSV file from the processed S3 bucket."""
    try:
        obj = _s3.get_object(Bucket=_PROCESSED_BUCKET, Key=key)
        content = obj['Body'].read().decode('utf-8')
        reader = csv.DictReader(content.splitlines())
        return list(reader)
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'NoSuchKey':
            _logger.warning("S3 CSV key not found: s3://%s/%s", _PROCESSED_BUCKET, key)
        else:
            _logger.error("S3 error reading CSV %s: %s", key, exc)
    except Exception as exc:
        _logger.error("Error reading S3 CSV %s: %s", key, exc)
    return []


def _list_processed_keys(suffix: str) -> List[str]:
    """List all objects in the processed prefix matching a suffix."""
    if not _s3:
        return []
    try:
        resp = _s3.list_objects_v2(Bucket=_PROCESSED_BUCKET, Prefix=_PROCESSED_PREFIX)
        return [
            obj['Key'] for obj in resp.get('Contents', [])
            if obj['Key'].endswith(suffix)
        ]
    except Exception as exc:
        _logger.error("S3 list error: %s", exc)
        return []


# ── Data readers ──────────────────────────────────────────────────────────────

def _read_unique_errors_data(conversion_dir: str) -> List[dict]:
    """
    Load unique-errors data.
    In AWS (PROCESSED_BUCKET set): reads latest *-errors.json from S3.
    Locally: reads from local Conversion/ directory.
    """
    # ── AWS path ──────────────────────────────────────────────────────────────
    if _S3_ENABLED and _s3:
        # Try the fixed key first (written by app.py's run_conversion_outputs via Lambda)
        fixed_key = f"{_PROCESSED_PREFIX}json/{UNIQUE_ERRORS_JSON_FILENAME}"
        data = _s3_get_json(fixed_key)
        if data is not None:
            _logger.info("Loaded unique errors from s3://%s/%s (%d entries)",
                         _PROCESSED_BUCKET, fixed_key, len(data))
            return data

        # Fall back: find the most recent *-errors.json the Lambda produced
        error_keys = sorted(_list_processed_keys('-errors.json'), reverse=True)
        if error_keys:
            data = _s3_get_json(error_keys[0])
            if data is not None:
                _logger.info("Loaded unique errors from s3://%s/%s (%d entries)",
                             _PROCESSED_BUCKET, error_keys[0], len(data))
                return data

        _logger.warning("No unique errors data found in S3 — dashboard will be empty. "
                        "Trigger /api/simulate-traffic then wait ~10s for Lambda to process.")
        return []

    # ── Local path (unchanged) ────────────────────────────────────────────────
    candidate_paths = [
        os.path.join(conversion_dir, UNIQUE_ERRORS_JSON_FILENAME),
        os.path.join(conversion_dir, LEGACY_UNIQUE_ERRORS_JSON_FILENAME),
    ]
    for json_path in candidate_paths:
        if not os.path.exists(json_path):
            continue
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            continue
    return []


def _read_csv_rows(conversion_dir: str, date_filter=None) -> List[Dict]:
    """
    Load CSV rows for date-filtered dashboard queries.
    In AWS: reads from S3. Locally: reads from Conversion/ dir.
    """
    if _S3_ENABLED and _s3:
        # Find most recent CSV produced by Lambda
        csv_keys = sorted(_list_processed_keys('.csv'), reverse=True)
        if csv_keys:
            rows = _s3_get_csv_rows(csv_keys[0])
            _logger.info("Loaded %d CSV rows from S3", len(rows))
            return rows
        return []

    # Local path
    csv_path = os.path.join(conversion_dir, 'converted_application_logs.csv')
    if not os.path.exists(csv_path):
        return []
    try:
        with open(csv_path, 'r', encoding='utf-8') as f:
            return list(csv.DictReader(f))
    except Exception as exc:
        _logger.error("Error reading local CSV: %s", exc)
        return []


# ── Date range helpers (unchanged) ────────────────────────────────────────────

def _resolve_preset(preset: str) -> Tuple[Optional[date], Optional[date]]:
    today = date.today()
    if preset == 'today':
        return today, today
    if preset == 'week':
        return today - timedelta(days=6), today
    if preset == 'month':
        return today.replace(day=1), today
    if preset == 'quarter':
        month = today.month - ((today.month - 1) % 3)
        return today.replace(month=month, day=1), today
    return None, None


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _apply_date_filter(
    rows: List[Dict],
    from_date: Optional[date],
    to_date: Optional[date],
) -> List[Dict]:
    if not from_date and not to_date:
        return rows
    filtered = []
    for row in rows:
        row_date = _parse_date(row.get('Date') or row.get('Timestamp', ''))
        if row_date is None:
            continue
        if from_date and row_date < from_date:
            continue
        if to_date and row_date > to_date:
            continue
        filtered.append(row)
    return filtered


# ── Aggregation (unchanged) ───────────────────────────────────────────────────

def _aggregate_rows(rows: List[Dict]) -> List[Dict]:
    grouped: Dict[str, Dict] = {}
    for row in rows:
        key = (
            row.get(STATUS_CODE_KEY, ''),
            row.get(ERROR_CODE_KEY, ''),
            row.get(DESCRIPTION_KEY, ''),
            row.get(API_KEY, ''),
        )
        if key not in grouped:
            grouped[key] = {
                STATUS_CODE_KEY: key[0],
                ERROR_CODE_KEY:  key[1],
                DESCRIPTION_KEY: key[2],
                API_KEY:         key[3],
                COUNT_KEY:       0,
                LAST_SEEN_KEY:   row.get('Timestamp', ''),
                'Dates':         [],
            }
        grouped[key][COUNT_KEY] += 1
        ts = row.get('Timestamp', '')
        if ts > grouped[key][LAST_SEEN_KEY]:
            grouped[key][LAST_SEEN_KEY] = ts
        d = row.get('Date', '')
        if d and d not in grouped[key]['Dates']:
            grouped[key]['Dates'].append(d)

    result = list(grouped.values())
    result.sort(key=lambda r: r[COUNT_KEY], reverse=True)
    return result


# ── Public API ────────────────────────────────────────────────────────────────

def build_dashboard_payload(
    conversion_dir: str,
    run_conversion_outputs,
    request_args,
) -> dict:
    """
    Build the complete dashboard payload.
    Reads from S3 in AWS, local files when running locally.
    """
    # Resolve date range
    preset   = request_args.get('preset', '')
    from_str = request_args.get('from')
    to_str   = request_args.get('to')

    if preset:
        from_date, to_date = _resolve_preset(preset)
    else:
        from_date = _parse_date(from_str)
        to_date   = _parse_date(to_str)

    has_date_filter = bool(from_date or to_date)

    if has_date_filter:
        # Date-filtered: aggregate from raw CSV rows
        if not _S3_ENABLED:
            run_conversion_outputs()
        raw_rows    = _read_csv_rows(conversion_dir)
        filtered    = _apply_date_filter(raw_rows, from_date, to_date)
        errors_data = _aggregate_rows(filtered)
    else:
        # No filter: use pre-aggregated unique errors JSON (fastest path)
        if not _S3_ENABLED:
            run_conversion_outputs()
        errors_data = _read_unique_errors_data(conversion_dir)

    total_errors  = sum(int(r.get(COUNT_KEY, 0)) for r in errors_data)
    unique_errors = len(errors_data)

    return {
        'errors':        errors_data,
        'total_errors':  total_errors,
        'unique_errors': unique_errors,
        'from_date':     from_date.isoformat() if from_date else None,
        'to_date':       to_date.isoformat()   if to_date   else None,
        's3_enabled':    _S3_ENABLED,
        'data_source':   f"s3://{_PROCESSED_BUCKET}/{_PROCESSED_PREFIX}" if _S3_ENABLED else "local",
    }
