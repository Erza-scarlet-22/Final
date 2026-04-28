# dummy-infra-app/log_shipper.py
#
# CHANGES vs your version:
#
#  1. S3 key is FIXED: raw-logs/application.log  (was dummy-app-TIMESTAMP.log)
#     WHY: The Lambda S3 notification filter expects raw-logs/application.log
#          exactly — the filter prefix is raw-logs/ and the Lambda test event
#          uses the key raw-logs/application.log.  A timestamped key still
#          triggers Lambda but breaks manual test events and is harder to track.
#
#  2. boto3 client is lazy-initialised on first ship() call, NOT at import time.
#     WHY: When running locally without AWS credentials, import would crash.
#          With lazy init the app starts fine and only fails if you actually
#          try to ship without a bucket configured.
#
#  3. Local copy: after every successful or skipped ship(), the events log is
#     also copied to  <log_dir>/application.log  so you can diff both files
#     locally without needing S3 access.
#     application.log  = what was actually shipped to S3 (or would be)
#     ssl_events.log   = live file being written to
#
#  Everything else is unchanged from your version.

import os
import shutil
from datetime import datetime, timezone

try:
    import boto3
    from botocore.exceptions import ClientError
    _BOTO3_AVAILABLE = True
except ImportError:
    _BOTO3_AVAILABLE = False


class LogShipper:
    """
    Ships ssl_events.log to S3 as raw-logs/application.log.
    Also copies to application.log locally for easy verification.
    Only uploads when new content has been written since the last ship.
    """

    # ── Fixed S3 key ─────────────────────────────────────────────────────────
    # Must match the S3 notification filter and Lambda test event key.
    # Changing this to a timestamped key will break manual Lambda tests.
    S3_OBJECT_NAME = "application.log"

    def __init__(self, logger, events_log_file: str):
        """
        events_log_file : path to ssl_events.log (errors + resolutions only)
        The local application.log copy is placed in the same directory.
        """
        self._logger           = logger
        self._events_log_file  = events_log_file
        self._log_dir          = os.path.dirname(events_log_file)

        # Local copy: <log_dir>/application.log — mirrors what goes to S3
        self._local_app_log    = os.path.join(self._log_dir, "application.log")

        # Lazy boto3 — initialised on first ship() call so local dev doesn't crash
        self._s3               = None
        self._bucket           = os.getenv("RAW_LOGS_BUCKET", "")
        self._prefix           = os.getenv("RAW_LOGS_PREFIX", "raw-logs/")
        self._last_shipped_pos = 0
        self.last_s3_key       = ""

    # ── boto3 lazy init ───────────────────────────────────────────────────────

    def _get_s3(self):
        """Return boto3 S3 client, initialising on first use."""
        if self._s3 is None:
            if not _BOTO3_AVAILABLE:
                raise RuntimeError(
                    "boto3 is not installed. Run: pip install boto3"
                )
            self._s3 = boto3.client(
                "s3",
                region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            )
        return self._s3

    # ── Local mirror ──────────────────────────────────────────────────────────

    def _update_local_copy(self):
        """
        Copy ssl_events.log → application.log in the same log directory.
        This gives you a local file you can open and inspect without S3.

        application.log  = stable copy of what was last shipped (or would be)
        ssl_events.log   = live append-only file still being written to
        """
        try:
            if os.path.exists(self._events_log_file):
                shutil.copy2(self._events_log_file, self._local_app_log)
                self._logger.debug(
                    "Local copy updated: %s → %s",
                    self._events_log_file, self._local_app_log,
                )
        except OSError as exc:
            # Non-fatal — local copy is for convenience only
            self._logger.warning("Could not update local application.log: %s", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def ship(self) -> bool:
        """
        Upload ssl_events.log to s3://<bucket>/raw-logs/application.log.

        Guards:
          - RAW_LOGS_BUCKET must be set (skipped in local dev with a warning)
          - Events log must exist and have new content since last ship
          - On success: updates _last_shipped_pos and local application.log copy
          - On failure: logs error and returns False (never raises)

        Returns True on successful S3 upload, False otherwise.
        """
        # Always update the local copy regardless of S3 outcome
        # so local dev always has an up-to-date application.log to inspect
        self._update_local_copy()

        if not self._bucket:
            self._logger.warning(
                "RAW_LOGS_BUCKET not set — S3 ship skipped. "
                "Set this env var to enable shipping. "
                "Local copy: %s", self._local_app_log,
            )
            return False

        if not os.path.exists(self._events_log_file):
            self._logger.warning(
                "Events log not found: %s — nothing to ship yet",
                self._events_log_file,
            )
            return False

        current_size = os.path.getsize(self._events_log_file)

        if current_size <= self._last_shipped_pos:
            self._logger.debug(
                "No new events since last ship (file=%d bytes, shipped_up_to=%d) — skipping",
                current_size, self._last_shipped_pos,
            )
            return False

        # Fixed S3 key — same on every upload.
        # Lambda S3 notification fires on every PUT regardless of key.
        # Using a fixed key means the test event in the Lambda console
        # always matches: "key": "raw-logs/application.log"
        s3_key = f"{self._prefix}{self.S3_OBJECT_NAME}"

        try:
            s3 = self._get_s3()
            s3.upload_file(
                self._events_log_file,
                self._bucket,
                s3_key,
                ExtraArgs={"ContentType": "text/plain"},
            )
            prev_pos               = self._last_shipped_pos
            self._last_shipped_pos = current_size
            self.last_s3_key       = s3_key

            self._logger.info(
                "Shipped %d bytes (delta: %d bytes) → s3://%s/%s",
                current_size,
                current_size - prev_pos,
                self._bucket,
                s3_key,
            )
            return True

        except Exception as exc:
            self._logger.error(
                "S3 upload failed for s3://%s/%s: %s",
                self._bucket, s3_key, exc,
            )
            return False

    def has_new_events(self) -> bool:
        """
        Return True if ssl_events.log has content not yet shipped.
        Used by the background loop to decide whether to call ship().
        """
        if not os.path.exists(self._events_log_file):
            return False
        return os.path.getsize(self._events_log_file) > self._last_shipped_pos
