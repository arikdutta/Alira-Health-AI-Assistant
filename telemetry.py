"""Privacy-safe structured telemetry for the assistant (Phase 5.1 / 5.2).

Every event lands in the Application Insights `customEvents` table and carries the same seven
dimensions, so the KQL dashboards can slice any of them the same way. Raw user ids and raw
utterances never pass through here: users are pseudonymised with an HMAC and utterances are
redacted first. The full utterance goes only to the Snowflake feedback table, under DW governance.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
import uuid

from opentelemetry import trace

QUERY_ROUTED = "QueryRouted"
LOW_CONFIDENCE_FALLBACK = "LowConfidenceFallback"
UNMAPPED_TERM = "UnmappedTerm"
QUERY_FAILED = "QueryFailed"

# The Azure Monitor log exporter sends a record carrying this attribute to customEvents instead of traces
CUSTOM_EVENT_NAME_ATTRIBUTE = "microsoft.custom_event.name"

# Tripwire for identifying fields: a dimension with one of these names is a bug, not a telemetry choice
FORBIDDEN_DIMENSIONS = frozenset({"UserEmail", "RawUtterance", "raw_utterance", "email", "oid", "preferred_username"})

WITHHELD = "[WITHHELD]"
COMPANY_PLACEHOLDER = "[COMPANY]"
NUMBER_PLACEHOLDER = "[NUM]"

# Currency amounts, percentages, years, "20M", "$1.5bn", "3 million" and plain digits alike
NUMBER_PATTERN = re.compile(
    r"(?:[$€£]\s?)?\d(?:[\d,.]*\d)?(?:\s?%|\s?(?:k|m|mm|bn|b|thousand|million|billion)\b)?",
    re.IGNORECASE,
)

logger = logging.getLogger("AliraAssistantLogger")
logger.setLevel(logging.INFO)

_ephemeral_key = secrets.token_bytes(32)


def pseudonymisation_key_configured() -> bool:
    return bool(os.environ.get("TELEMETRY_HMAC_SECRET"))


def user_hash(user_id: str) -> str:
    """HMAC-SHA256 of the user's Entra object id; the reverse mapping lives only in the identity store.

    Without TELEMETRY_HMAC_SECRET (local runs, tests) a per-process random key is used, so hashes are
    still unlinkable to the user but change on every restart. main.py refuses to export telemetry then.
    """
    configured = os.environ.get("TELEMETRY_HMAC_SECRET")
    key = configured.encode() if configured else _ephemeral_key
    return hmac.new(key, user_id.encode(), hashlib.sha256).hexdigest()


def current_trace_id() -> str:
    """The active OpenTelemetry trace id (the App Insights operation_Id), or a fresh id when tracing is off."""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else uuid.uuid4().hex


class UtteranceRedactor:
    """Strips company names and numeric figures from text before it reaches telemetry.

    Company names come from the warehouse master list, loaded in the background and refreshed hourly so a
    name added by a deal team is redacted without a redeploy. Until the first load succeeds, text is
    withheld entirely: an unredacted confidential target name is the failure this class exists to prevent.
    """

    def __init__(self, load_names, refresh_after_s: float = 3600, retry_after_s: float = 60):
        self._load_names = load_names
        self._refresh_after_s = refresh_after_s
        self._retry_after_s = retry_after_s
        self._names_pattern = None
        self._loaded_at = None
        self._next_attempt_at = 0.0
        self._refreshing = threading.Lock()

    def set_names(self, names) -> None:
        # Longest first, so "Kura Therapeutics DE" is replaced whole rather than leaving " DE" behind
        escaped = sorted(
            (re.escape(name.strip()).replace(r"\ ", r"\s+") for name in names if name and len(name.strip()) > 1),
            key=len, reverse=True,
        )
        self._names_pattern = re.compile(rf"(?<!\w)(?:{'|'.join(escaped)})(?!\w)", re.IGNORECASE) if escaped else None
        self._loaded_at = time.monotonic()

    def refresh_in_background(self) -> None:
        if self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._refresh, name="alira-redaction-refresh", daemon=True).start()

    def _refresh(self) -> None:
        try:
            self.set_names(self._load_names())
        except Exception as exc:
            # Keep the previous list, if any, and back off so a warehouse outage isn't hit on every event
            self._next_attempt_at = time.monotonic() + self._retry_after_s
            logger.warning("Company master list refresh failed: %s", type(exc).__name__)
        finally:
            self._refreshing.release()

    def redact(self, text: str) -> str:
        now = time.monotonic()
        stale = self._loaded_at is None or now - self._loaded_at > self._refresh_after_s
        if stale and now >= self._next_attempt_at:
            self.refresh_in_background()
        if self._loaded_at is None:
            return WITHHELD
        if self._names_pattern is not None:
            text = self._names_pattern.sub(COMPANY_PLACEHOLDER, text)
        return NUMBER_PATTERN.sub(NUMBER_PLACEHOLDER, text)


def emit_event(
    name: str,
    *,
    trace_id: str,
    practice: str,
    intent: str,
    confidence: float,
    latency_ms: int,
    row_count: int,
    user_hash: str,
    level: int = logging.INFO,
    **extra,
) -> None:
    """Sends one structured event to Application Insights customEvents."""
    forbidden = FORBIDDEN_DIMENSIONS & extra.keys()
    if forbidden:
        raise ValueError(f"Identifying fields must not be sent to telemetry: {sorted(forbidden)}")
    dimensions = {
        "trace_id": trace_id,
        "practice": practice,
        "intent": intent,
        "confidence": round(confidence, 4),
        "latency_ms": latency_ms,
        "row_count": row_count,
        "user_hash": user_hash,
        # OpenTelemetry rejects None attribute values
        **{key: value for key, value in extra.items() if value is not None},
    }
    logger.log(level, name, extra={CUSTOM_EVENT_NAME_ATTRIBUTE: name, **dimensions})
