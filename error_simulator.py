# dummy-infra-app/error_simulator.py
#
# CHANGES vs your version:
#
#  1. _write_lines() now auto-creates the parent directory if it doesn't exist.
#     WHY: When running locally for the first time the logs/ directory may not
#          exist yet. This makes `python app.py` work out-of-the-box without
#          needing a manual mkdir.
#
#  2. Added VALID_TYPES as a class constant so app.py can reference it directly
#     instead of hard-coding the list in two places.
#
#  Everything else is identical to your version — log format, error definitions,
#  resolution messages, and the 3-line write cycle are all unchanged.

import random
from datetime import datetime, timezone


class ErrorSimulator:
    """
    Writes structured error / resolution log entries to ssl_events.log.
    Format matches log_parser.py regex patterns so Lambda parses them correctly.

    3-line cycle per error (required by log_parser.py):
      Line 1: [TS] [INFO]  <api> IP: <ip>              ← API_WITH_IP_PATTERN
      Line 2: [TS] [ERROR] <description> {'error_code': N}  ← ERROR_CODE_PATTERN
      Line 3: [TS] [INFO]  <api> Status Code: N         ← STATUS_PATTERN → emits row
    """

    # ── Error catalogue ───────────────────────────────────────────────────────
    # (http_status_code, error_code, description, api_path)
    ERROR_DEFINITIONS = {
        "ssl_expired": (
            495, 9010,
            "SSL certificate expired for domain api.dummy-app.internal",
            "GET /api/dummy/status",
        ),
        "ssl_expiring": (
            200, 9011,
            "SSL certificate expires in 7 days for domain api.dummy-app.internal",
            "GET /api/dummy/status",
        ),
        "password_expired": (
            401, 9012,
            "Service account password expired, authentication failed",
            "POST /api/dummy/auth",
        ),
        "db_storage": (
            507, 9013,
            "Database storage at 92% capacity, writes may fail",
            "POST /api/dummy/db-write",
        ),
        "db_connection": (
            504, 9014,
            "RDS connection pool exhausted, timeout after 30s",
            "GET /api/dummy/db-read",
        ),
        "compute_overload": (
            503, 9015,
            "CPU at 95%, memory at 88%, dropping requests",
            "POST /api/dummy/process",
        ),
    }

    RESOLUTION_MESSAGES = {
        "ssl_expired":      "SSL certificate renewed successfully. New cert ARN stored in Secrets Manager.",
        "ssl_expiring":     "SSL certificate rotated proactively. 90 days until next expiry.",
        "password_expired": "Service account password rotated via Secrets Manager. Auth reconnected.",
        "db_storage":       "RDS allocated storage increased. New capacity applied successfully.",
        "db_connection":    "RDS instance class upgraded. Connection pool limits increased.",
        "compute_overload": "ECS desired count increased. Additional tasks launched and healthy.",
    }

    # Convenience: valid type list referenced by app.py validation
    VALID_TYPES = list(ERROR_DEFINITIONS.keys())

    def __init__(self, logger, events_log_file: str):
        """
        logger          : Python logger (writes to app.log, NOT to events log)
        events_log_file : path to ssl_events.log (errors + resolutions only)
        """
        self._logger          = logger
        self._events_log_file = events_log_file

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _fake_ip(self) -> str:
        return (
            f"10.{random.randint(0, 255)}"
            f".{random.randint(0, 255)}"
            f".{random.randint(1, 254)}"
        )

    def _write_lines(self, lines: list) -> None:
        """
        Append lines to ssl_events.log.
        Auto-creates the parent directory if it doesn't exist — this makes
        local dev work without a manual mkdir.
        """
        try:
            os.makedirs(os.path.dirname(self._events_log_file), exist_ok=True)
            with open(self._events_log_file, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
        except OSError as exc:
            self._logger.error(
                "Failed to write events log %s: %s",
                self._events_log_file, exc,
            )
            raise

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_error(self, error_type: str) -> str:
        """
        Write a 3-line error cycle to ssl_events.log.
        Returns line 2 (the ERROR line containing description + error_code).
        """
        if error_type not in self.ERROR_DEFINITIONS:
            raise ValueError(
                f"Unknown error_type: '{error_type}'. "
                f"Valid types: {self.VALID_TYPES}"
            )

        http_code, err_code, description, api_path = self.ERROR_DEFINITIONS[error_type]
        ts = self._ts()
        ip = self._fake_ip()

        line1 = f"[{ts}] [INFO] {api_path} IP: {ip}"
        line2 = f"[{ts}] [ERROR] {description} {{'error_code': {err_code}}}"
        line3 = f"[{ts}] [INFO] {api_path} Status Code: {http_code}"

        self._write_lines([line1, line2, line3])

        self._logger.info(
            "Event written → %s | type=%s http=%d code=%d",
            self._events_log_file, error_type, http_code, err_code,
        )
        return line2

    def generate_resolution(self, error_type: str, details: dict) -> str:
        """
        Write a RESOLVED line to ssl_events.log and return it.
        Called by the /resolve endpoint after a Lambda action group fixes an error.
        """
        ts  = self._ts()
        msg = self.RESOLUTION_MESSAGES.get(
            error_type, f"{error_type} resolved successfully."
        )

        # Append cert ARN if present (trimmed to last 30 chars for readability)
        cert = (details or {}).get("cert_arn", "")
        if cert:
            msg += f" Cert ARN: ...{cert[-30:]}"

        line = f"[{ts}] [INFO] RESOLVED: {msg}"
        self._write_lines([line])

        self._logger.info(
            "Resolution written → %s | type=%s",
            self._events_log_file, error_type,
        )
        return line


# ── Missing import fix ────────────────────────────────────────────────────────
import os  # noqa: E402  (needed by _write_lines, placed here to keep class clean)