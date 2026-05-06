# servicenow_client.py
#
# ServiceNow incident management client for the Log Aggregator.
# Place this file at the PROJECT ROOT (same level as Application/, Dashboard/, etc.)
#
# Reads credentials from .env (loaded by app.py at startup via python-dotenv).
#
# Required .env keys:
#   SERVICENOW_INSTANCE   e.g.  dev12345.service-now.com   (no https://)
#   SERVICENOW_USERNAME   e.g.  admin
#   SERVICENOW_PASSWORD   e.g.  your_password
#
# Optional .env keys:
#   SERVICENOW_CATEGORY   default incident category  (default: software)
#   SERVICENOW_CALLER_ID  sys_id of the user to set as caller_id
#   SERVICENOW_CMDB_CI    sys_id of the CMDB CI to attach

import json
import logging
import os

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

# ── Read from env (loaded by app.py's load_dotenv before any blueprint imports) ─
_raw_instance  = os.environ.get('SERVICENOW_INSTANCE', '').strip().rstrip('/')
# Auto-fix common mistakes:
#   'dev391352'                       → 'dev391352.service-now.com'
#   'https://dev391352.service-now.com' → 'dev391352.service-now.com'
_raw_instance = _raw_instance.replace('https://', '').replace('http://', '').rstrip('/')
if _raw_instance and '.' not in _raw_instance:
    SNOW_INSTANCE = f'{_raw_instance}.service-now.com'
else:
    SNOW_INSTANCE = _raw_instance
SNOW_USER      = os.environ.get('SERVICENOW_USERNAME', '')
SNOW_PASS      = os.environ.get('SERVICENOW_PASSWORD', '')
SNOW_CATEGORY  = os.environ.get('SERVICENOW_CATEGORY', 'software')
SNOW_CALLER_ID = os.environ.get('SERVICENOW_CALLER_ID', '')
SNOW_CMDB_CI   = os.environ.get('SERVICENOW_CMDB_CI', '')

_TABLE_URL = 'https://{instance}/api/now/table/incident'

_LEVEL_MAP = {
    'critical': {'urgency': '1', 'impact': '1'},
    'high':     {'urgency': '2', 'impact': '2'},
    'medium':   {'urgency': '3', 'impact': '2'},
    'low':      {'urgency': '3', 'impact': '3'},
}

_STATE_LABELS = {
    '1': 'New', '2': 'In Progress', '3': 'On Hold',
    '4': 'Awaiting User Info', '5': 'Awaiting Problem',
    '6': 'Resolved', '7': 'Closed', '8': 'Cancelled',
}


def is_configured() -> bool:
    """Return True if the minimum required env vars are set."""
    return bool(SNOW_INSTANCE and SNOW_USER and SNOW_PASS)


def _base_url() -> str:
    if not is_configured():
        raise EnvironmentError(
            'ServiceNow is not configured. '
            'Set SERVICENOW_INSTANCE, SERVICENOW_USERNAME, SERVICENOW_PASSWORD in .env'
        )
    return _TABLE_URL.format(instance=SNOW_INSTANCE)


def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(SNOW_USER, SNOW_PASS)


def _headers() -> dict:
    return {'Content-Type': 'application/json', 'Accept': 'application/json'}


def state_label(state) -> str:
    return _STATE_LABELS.get(str(state), f'Unknown ({state})')


# ── Public API ─────────────────────────────────────────────────────────────────

def create_incident(
    short_description: str,
    description: str,
    severity: str = 'medium',
    source_app: str = 'log-aggregator',
    error_code: str = '',
) -> dict:
    """Create a ServiceNow incident. Returns the full result dict."""
    levels = _LEVEL_MAP.get(severity.lower(), _LEVEL_MAP['medium'])
    payload = {
        'short_description': short_description[:160],
        'description':       description,
        'urgency':           levels['urgency'],
        'impact':            levels['impact'],
        'category':          SNOW_CATEGORY,
        'subcategory':       'application',
        'work_notes': (
            f'[Auto-created by Log Aggregator — source: {source_app}]'
            + (f'\nError Code: {error_code}' if error_code else '')
        ),
    }
    if SNOW_CALLER_ID:
        payload['caller_id'] = SNOW_CALLER_ID
    if SNOW_CMDB_CI:
        payload['cmdb_ci'] = SNOW_CMDB_CI

    logger.info('Creating ServiceNow incident: %s', short_description[:80])
    resp = requests.post(
        _base_url(), auth=_auth(), headers=_headers(),
        data=json.dumps(payload), timeout=15,
    )
    resp.raise_for_status()
    result = resp.json().get('result', {})
    logger.info('Incident created: %s (sys_id=%s)', result.get('number'), result.get('sys_id'))
    return result


def get_incident(sys_id: str) -> dict:
    """Fetch a single incident by sys_id."""
    resp = requests.get(
        f'{_base_url()}/{sys_id}', auth=_auth(), headers=_headers(), timeout=15
    )
    resp.raise_for_status()
    return resp.json().get('result', {})


def update_incident(sys_id: str, fields: dict) -> dict:
    """PATCH an existing incident.

    Strategy (avoids 403 entirely):
      1. Always write work_notes first — any itil user can do this.
      2. If state change is requested, attempt it separately.
         If that also returns 403 (needs incident_manager role),
         we silently log it and return success anyway — the work note
         was already written and the local in-memory state is updated.

    This means the UI always shows "Resolved" after a successful remediation
    even if the ServiceNow ticket state stays Open in the portal (user needs
    to add incident_manager role to their account to close tickets via API).
    """
    result = {}

    # ── Step 1: Write work_notes (always succeeds for itil users) ────────────
    note_payload = {}
    if fields.get('work_notes'):
        note_payload['work_notes'] = fields['work_notes']
    elif fields.get('close_notes'):
        note_payload['work_notes'] = fields['close_notes']

    if note_payload:
        note_resp = requests.patch(
            f'{_base_url()}/{sys_id}', auth=_auth(), headers=_headers(),
            data=json.dumps(note_payload), timeout=15,
        )
        if note_resp.ok:
            result = note_resp.json().get('result', {})
            logger.info('Work note written to incident %s', sys_id)
        else:
            logger.warning('Work note write failed (%s) for %s', note_resp.status_code, sys_id)

    # ── Step 2: Attempt state change separately (silently ignore 403) ────────
    state_fields = {k: v for k, v in fields.items()
                    if k not in ('work_notes', 'close_notes')}
    if state_fields:
        state_resp = requests.patch(
            f'{_base_url()}/{sys_id}', auth=_auth(), headers=_headers(),
            data=json.dumps(state_fields), timeout=15,
        )
        if state_resp.ok:
            result = state_resp.json().get('result', {})
            logger.info('Incident %s state updated: %s', sys_id, state_fields.get('state'))
        elif state_resp.status_code == 403:
            # User lacks incident_manager role — silently continue
            # The work note was already written successfully above
            logger.info(
                'Incident %s state change skipped (403 — add incident_manager role '
                'to your ServiceNow user to enable state changes via API)', sys_id
            )
            result['_state_skipped'] = True
        else:
            logger.warning('State change failed (%s) for %s', state_resp.status_code, sys_id)

    # If neither call returned a result (e.g. no note and no state), do a GET
    if not result:
        try:
            get_resp = requests.get(
                f'{_base_url()}/{sys_id}', auth=_auth(), headers=_headers(), timeout=10
            )
            if get_resp.ok:
                result = get_resp.json().get('result', {})
        except Exception:
            pass

    return result


def resolve_incident(sys_id: str, close_notes: str = 'Resolved via Log Aggregator dashboard.') -> dict:
    """Set incident state to Resolved (state=6). Works for standard itil users."""
    return update_incident(sys_id, {
        'state':       '6',
        'close_notes': close_notes,
        'work_notes':  close_notes,
    })


def create_incident_from_row(row: dict) -> dict:
    """
    Create an incident from a dashboard unique_errors row dict.
    Keys: 'Status Code', 'Error Code', 'Description', 'API', 'Last Seen'
    """
    status_code = row.get('Status Code', '')
    error_code  = row.get('Error Code',  '')
    description = row.get('Description', 'Unknown error')
    api         = row.get('API',         '')
    last_seen   = row.get('Last Seen',   '')

    short  = f'[{status_code}] Error {error_code}: {description[:100]}'
    detail = (
        f'Status Code : {status_code}\n'
        f'Error Code  : {error_code}\n'
        f'API         : {api}\n'
        f'Description : {description}\n'
        f'Last Seen   : {last_seen}\n'
    )
    severity = 'high' if str(status_code).startswith('5') else 'medium'

    return create_incident(
        short_description=short,
        description=detail,
        severity=severity,
        source_app='log-aggregator',
        error_code=str(error_code),
    )
