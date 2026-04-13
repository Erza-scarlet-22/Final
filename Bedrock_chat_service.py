# AWS Bedrock Agent integration for Log Aggregator chat insights.
#
# ── How credentials work in this file ────────────────────────────────────────
#
#  IN AWS (ECS):
#    CloudFormation wires Secrets Manager → ECS task definition Secrets block.
#    ECS injects BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID as plain env vars
#    before the container starts. This file reads them with os.getenv() — same
#    as before — but the values now come from Secrets Manager, not a .env file.
#    No boto3 Secrets Manager call is needed inside this file for that path.
#
#    If for any reason the env vars are empty (e.g. the task def secrets block
#    is missing), this file falls back to a direct Secrets Manager fetch using
#    the secret name stored in AWS_SECRETS_NAME env var.
#
#  LOCALLY:
#    Set BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID in your .env file.
#    app.py loads .env at startup so os.getenv() picks them up.
#    No AWS calls are made during local development.
#
# ── Credential resolution order ──────────────────────────────────────────────
#
#   1. os.getenv("BEDROCK_AGENT_ID") + os.getenv("BEDROCK_AGENT_ALIAS_ID")
#      → Set automatically by ECS from Secrets Manager (primary AWS path)
#      → Set from .env file for local development
#
#   2. AWS Secrets Manager direct fetch
#      → Only used when env vars are empty AND AWS_SECRETS_NAME is set
#      → Fetched once and cached for the container lifetime
#
#   3. RuntimeError raised with a clear message if neither source works
#
# ── Secret structure in Secrets Manager ──────────────────────────────────────
#
#   Secret name : log-aggregator/bedrock-<EnvironmentName>
#   Secret value:
#   {
#     "BEDROCK_AGENT_ID":       "your-real-agent-id",
#     "BEDROCK_AGENT_ALIAS_ID": "your-real-alias-id"
#   }
#
# ── Required env vars ────────────────────────────────────────────────────────
#
#   BEDROCK_AGENT_ID       — Bedrock Agent ID (injected by ECS or set in .env)
#   BEDROCK_AGENT_ALIAS_ID — Bedrock Agent Alias ID (injected by ECS or .env)
#   AWS_DEFAULT_REGION     — AWS region (default: us-east-1)
#
# ── Optional env vars ────────────────────────────────────────────────────────
#
#   AWS_SECRETS_NAME         — Secrets Manager secret name for fallback fetch
#                              e.g. "log-aggregator/bedrock-dev"
#   BEDROCK_AGENT_SESSION_ID — Reuse an existing Bedrock session across calls

import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Secrets Manager credential cache ─────────────────────────────────────────
# Populated once on first use. None means "not yet fetched".
# Empty dict means "fetch was attempted but returned nothing useful".
_sm_cache: Optional[Dict[str, str]] = None


def _fetch_from_secrets_manager(secret_name: str) -> Dict[str, str]:
    """
    Fetch and parse the Bedrock secret from AWS Secrets Manager.
    Returns a dict with BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID.
    Returns empty dict on any error (caller handles the fallback).
    Result is cached for the container lifetime — SM is only called once.
    """
    global _sm_cache
    if _sm_cache is not None:
        return _sm_cache

    try:
        import boto3
        region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)

        logger.info("Fetching Bedrock credentials from Secrets Manager: %s", secret_name)
        response = client.get_secret_value(SecretId=secret_name)
        secret_data = json.loads(response["SecretString"])

        _sm_cache = {
            "BEDROCK_AGENT_ID":       secret_data.get("BEDROCK_AGENT_ID", "").strip(),
            "BEDROCK_AGENT_ALIAS_ID": secret_data.get("BEDROCK_AGENT_ALIAS_ID", "").strip(),
        }
        logger.info(
            "Bedrock credentials loaded from Secrets Manager. "
            "Agent ID present: %s, Alias ID present: %s",
            bool(_sm_cache["BEDROCK_AGENT_ID"]),
            bool(_sm_cache["BEDROCK_AGENT_ALIAS_ID"]),
        )
        return _sm_cache

    except Exception as exc:
        logger.error(
            "Failed to fetch Bedrock credentials from Secrets Manager (%s): %s",
            secret_name, exc,
        )
        _sm_cache = {}
        return {}


def _resolve_credentials() -> Tuple[str, str]:
    """
    Resolve BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID.

    Priority:
      1. Environment variables (set by ECS task def Secrets block in AWS,
         or by .env file in local dev)
      2. Direct Secrets Manager fetch (fallback — only when AWS_SECRETS_NAME is set
         and env vars are empty)
    """
    # ── Priority 1: env vars ──────────────────────────────────────────────────
    # In AWS: ECS injects these from Secrets Manager before container starts.
    # Locally: loaded from .env by app.py at startup.
    agent_id       = os.getenv("BEDROCK_AGENT_ID", "").strip()
    agent_alias_id = os.getenv("BEDROCK_AGENT_ALIAS_ID", "").strip()

    if agent_id and agent_alias_id:
        logger.debug("Bedrock credentials resolved from environment variables.")
        return agent_id, agent_alias_id

    # ── Priority 2: direct Secrets Manager fetch ──────────────────────────────
    # Fallback for when env vars are not populated.
    # AWS_SECRETS_NAME should be set to: log-aggregator/bedrock-<env>
    secret_name = os.getenv("AWS_SECRETS_NAME", "").strip()
    if secret_name:
        sm_data = _fetch_from_secrets_manager(secret_name)
        agent_id       = sm_data.get("BEDROCK_AGENT_ID", "")
        agent_alias_id = sm_data.get("BEDROCK_AGENT_ALIAS_ID", "")
        if agent_id and agent_alias_id:
            logger.debug("Bedrock credentials resolved from Secrets Manager.")
            return agent_id, agent_alias_id

    # ── Neither source worked ────────────────────────────────────────────────
    # Log a clear message to help diagnose the issue.
    logger.error(
        "Bedrock credentials not found. "
        "In AWS: check that the ECS task definition Secrets block is set. "
        "In local dev: set BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID in .env. "
        "AWS_SECRETS_NAME env var = '%s'",
        os.getenv("AWS_SECRETS_NAME", "(not set)"),
    )
    return "", ""


# ── Prompt builder (unchanged from original) ─────────────────────────────────

def _build_agent_input(
    error_details: Dict[str, object],
    user_message: str,
    history: List[Dict[str, str]],
) -> str:
    """Compose the full text input for the Bedrock agent.

    Combines the structured error context, the last 8 turns of conversation
    history, and the current user question into a single prompt string.
    """
    history_lines: List[str] = []
    for item in history[-8:]:
        role = (item.get("role") or "user").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = (item.get("content") or "").strip()
        if not text:
            continue
        label = "User" if role == "user" else "Assistant"
        history_lines.append(f"{label}: {text}")

    error_context = (
        "Selected error context:\n"
        f"- Status Code: {error_details.get('Status Code', '')}\n"
        f"- Error Code: {error_details.get('Error Code', '')}\n"
        f"- Description: {error_details.get('Description', '')}\n"
        f"- API: {error_details.get('API', '')}\n"
        f"- Count: {error_details.get('Count', '')}\n"
        f"- Last Seen: {error_details.get('Last Seen', '')}\n"
        f"- Dates: {error_details.get('Dates', '')}\n"
    )

    history_block = "\n".join(history_lines) if history_lines else "No prior chat history."

    return (
        "You are helping with API incident triage. Provide concise root cause analysis, "
        "immediate checks, remediation, and prevention. "
        "Respond in plain text only and do not output function call plans, JSON, XML, or tool invocation blocks.\n\n"
        f"{error_context}\n"
        f"Conversation so far:\n{history_block}\n\n"
        f"User question:\n{user_message.strip()}"
    )


# ── Response parsers (unchanged from original) ────────────────────────────────

def _decode_json_objects(text: str) -> List[Dict[str, object]]:
    """Decode one or more JSON objects from a text blob."""
    decoder = json.JSONDecoder()
    objects: List[Dict[str, object]] = []
    parse_index = 0
    text_length = len(text)

    while parse_index < text_length:
        while parse_index < text_length and text[parse_index].isspace():
            parse_index += 1
        if parse_index >= text_length:
            break
        if text[parse_index] != "{":
            break
        try:
            value, next_parse_index = decoder.raw_decode(text, parse_index)
        except Exception:
            break
        if isinstance(value, dict):
            objects.append(value)
        parse_index = next_parse_index

    return objects


def _normalize_agent_reply(reply_text: str) -> str:
    """Convert raw tool-call payloads into a user-friendly explanation."""
    text = (reply_text or "").strip()
    if not text or "function_calls" not in text:
        return text

    decoded = _decode_json_objects(text)
    if not decoded:
        return text

    function_names: List[str] = []
    for obj in decoded:
        calls = obj.get("function_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            if name and name not in function_names:
                function_names.append(name)

    if not function_names:
        return text

    readable_list = ", ".join(function_names)
    return (
        "The Bedrock agent returned an internal tool-call plan instead of a final answer. "
        f"Requested functions: {readable_list}. "
        "This usually means your Bedrock Agent action group orchestration is enabled but "
        "not fully wired in this app. Ask a direct plain-language question again, or "
        "configure the agent to return final responses without tool-call output."
    )


def _extract_completion_text(response: Dict[str, object]) -> str:
    """Iterate over the streaming completion and concatenate all text chunks."""
    completion_stream = response.get("completion")
    if completion_stream is None:
        return ""

    text_parts: List[str] = []
    try:
        for event in completion_stream:
            chunk = event.get("chunk") if isinstance(event, dict) else None
            if not isinstance(chunk, dict):
                continue
            data = chunk.get("bytes")
            if data is None:
                continue
            if isinstance(data, bytes):
                text_parts.append(data.decode("utf-8", errors="replace"))
            else:
                text_parts.append(str(data))
    except Exception:
        return ""

    return "".join(text_parts).strip()


# ── Public API ────────────────────────────────────────────────────────────────

def generate_error_insight(
    error_details: Dict[str, object],
    user_message: str,
    history: List[Dict[str, str]],
    session_id: Optional[str] = None,
) -> Tuple[str, Dict[str, str]]:
    """
    Invoke the configured AWS Bedrock Agent with the given error context.

    Credentials are resolved via _resolve_credentials():
      1. ECS injected env vars from Secrets Manager (AWS production path)
      2. Direct Secrets Manager fetch via AWS_SECRETS_NAME (fallback)
      3. .env file values (local development)

    Args:
        error_details: Dashboard row dict for the selected error.
        user_message:  Question submitted by the user in the chat UI.
        history:       Prior turns as [{role, content}] dicts.
        session_id:    Optional Bedrock session ID for conversation continuity.

    Returns:
        (reply_text, metadata_dict)

    Raises:
        RuntimeError: If credentials are missing or the Bedrock call fails.
    """
    try:
        import boto3
    except Exception as exc:
        raise RuntimeError(f"boto3 is not installed: {exc}") from exc

    # Resolve credentials (env vars → Secrets Manager → error)
    agent_id, agent_alias_id = _resolve_credentials()

    if not agent_id or not agent_alias_id:
        raise RuntimeError(
            "Bedrock Agent credentials are not configured. "
            "In AWS: verify the ECS task definition Secrets block has BEDROCK_AGENT_ID "
            "and BEDROCK_AGENT_ALIAS_ID pointing to the correct Secrets Manager secret. "
            "In local dev: add BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID to your .env file."
        )

    region = os.getenv("AWS_DEFAULT_REGION", os.getenv("AWS_REGION", "us-east-1"))
    client = boto3.client("bedrock-agent-runtime", region_name=region)

    effective_session_id = (
        (session_id or "").strip()
        or os.getenv("BEDROCK_AGENT_SESSION_ID", "").strip()
        or str(uuid4())
    )

    input_text = _build_agent_input(error_details, user_message, history)

    logger.info(
        "Invoking Bedrock agent — agent_id=%s alias=%s session=%s region=%s",
        agent_id, agent_alias_id, effective_session_id, region,
    )

    try:
        last_exc = None
        for attempt in range(3):
            try:
                response = client.invoke_agent(
                    agentId=agent_id,
                    agentAliasId=agent_alias_id,
                    sessionId=effective_session_id,
                    inputText=input_text,
                )
                break
            except Exception as _exc:
                last_exc = _exc
                logger.warning("Bedrock attempt %d failed: %s", attempt + 1, _exc)
                if attempt < 2:
                    time.sleep(2 ** attempt)   # 1 s, 2 s backoff
        else:
            raise last_exc

        reply_text = _extract_completion_text(response)
        if not reply_text:
            reply_text = "No response text returned by Bedrock agent."
        reply_text = _normalize_agent_reply(reply_text)

        return reply_text, {
            "model_id":   f"agent:{agent_id}/{agent_alias_id}",
            "region":     region,
            "session_id": effective_session_id,
        }

    except Exception as exc:
        raise RuntimeError(f"Bedrock agent request failed: {exc}") from exc
