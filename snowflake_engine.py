import os
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
import snowflake.connector
from azure.core.credentials import AzureKeyCredential
from azure.ai.language.conversations import ConversationAnalysisClient

import foundry_orchestrator
import telemetry

# Initialize clients using safe fallback strings for local tests
AZURE_ENDPOINT = os.environ.get("AZURE_LANGUAGE_ENDPOINT", "http://localhost:8000")
AZURE_KEY = os.environ.get("AZURE_LANGUAGE_KEY", "mock-key")

language_client = ConversationAnalysisClient(
    endpoint=AZURE_ENDPOINT,
    credential=AzureKeyCredential(AZURE_KEY)
)

logger = logging.getLogger("AliraAssistantLogger")
logger.setLevel(logging.INFO)

# CLU entities that filter through vocabulary_synonyms, mapped to their synonym category
SYNONYM_CATEGORIES = {"TherapeuticArea": "THERAPEUTIC_AREA", "Geography": "GEOGRAPHY"}
# Practices the orchestrator hands to the Foundry agent, with the result type the web portal renders them as
AGENT_PRACTICES = {"Alira-MarketAccess": "MARKET_ACCESS", "Alira-RWE": "RWE_SEARCH"}

LOW_CONFIDENCE_MESSAGE = "I couldn't match your request to an Alira practice area."
UNCLEAR_INTENT_MESSAGE = "I couldn't work out what you're asking for. Please rephrase your request."

# Feedback and identity-map writes run off the request path: a failed write must never fail a query
_background = ThreadPoolExecutor(max_workers=2, thread_name_prefix="alira-warehouse-writer")
_registered_user_hashes: set[str] = set()


@dataclass
class _QueryTrace:
    """Accumulates the dimensions shared by every telemetry event for one consultant query."""
    trace_id: str
    user_hash: str
    utterance: str
    practice: str = ""
    intent: str = ""
    confidence: float = 0.0
    started: float = field(default_factory=time.perf_counter)

    def emit(self, event: str, row_count: int = 0, level: int = logging.INFO, with_utterance: bool = False, **extra):
        if with_utterance:
            extra["redacted_utterance"] = redactor.redact(self.utterance)
        telemetry.emit_event(
            event,
            trace_id=self.trace_id,
            practice=self.practice,
            intent=self.intent,
            confidence=self.confidence,
            latency_ms=round((time.perf_counter() - self.started) * 1000),
            row_count=row_count,
            user_hash=self.user_hash,
            level=level,
            **extra,
        )


def query_alira_assistant(user_prompt: str, user_context: dict = None, trace_id: str = None):
    """Processes prompts via Azure Orchestrator and retrieves data from Snowflake."""
    query = _QueryTrace(
        trace_id=trace_id or telemetry.current_trace_id(),
        user_hash=pseudonymise_user(user_context),
        utterance=user_prompt,
    )
    try:
        return _answer(user_prompt, user_context, query)
    except Exception as exc:
        # The exception type only: messages from Azure or Snowflake can echo query text
        query.emit(telemetry.QUERY_FAILED, level=logging.ERROR, with_utterance=True, error_type=type(exc).__name__)
        raise


def _answer(user_prompt: str, user_context: dict, query: _QueryTrace):
    response = language_client.analyze_conversation(
        task={
            "kind": "Conversation",
            "analysisInput": {
                "conversationItem": {
                    "participantId": "user",
                    "id": "1",
                    "text": user_prompt
                }
            },
            "parameters": {
                "projectName": "Alira-Master-Orchestrator",
                "deploymentName": "production-v1"
            }
        }
    )

    prediction = response["result"]["prediction"]
    routed_intent = prediction["topIntent"]
    practice_scores = {name: intent.get("confidenceScore", 0.0) for name, intent in prediction["intents"].items()}
    query.practice = routed_intent

    # The orchestrator falls back to "None" when no practice area clears its confidence threshold
    if routed_intent == "None":
        candidate = _best_candidate(practice_scores)
        query.intent = "None"
        query.confidence = practice_scores.get(candidate or "None", 0.0)
        query.emit(telemetry.LOW_CONFIDENCE_FALLBACK, level=logging.WARNING, with_utterance=True,
                   candidate_practice=candidate)
        # The child intent is unknown at this level, so the steward assigns one before approving
        record_feedback(query.trace_id, user_prompt, "LOW_CONFIDENCE")
        return {"message": LOW_CONFIDENCE_MESSAGE}

    query.confidence = practice_scores.get(routed_intent, 0.0)

    if routed_intent in AGENT_PRACTICES:
        return _ask_agent(user_prompt, routed_intent, query)

    # The orchestrator nests the routed child project's own prediction under the winning intent
    child_prediction = prediction["intents"][routed_intent].get("result", {}).get("prediction", {})
    child_intent = child_prediction.get("topIntent")
    intent_scores = {i["category"]: i.get("confidenceScore", 0.0) for i in child_prediction.get("intents", [])}
    entities_list = child_prediction.get("entities", [])
    query.intent = child_intent or "None"

    entities = {e["category"]: e["text"] for e in entities_list}

    # The child model falls back to "None" the same way when no intent clears its own threshold
    if child_intent in (None, "None"):
        suggested_intent = _best_candidate(intent_scores)
        query.emit(telemetry.LOW_CONFIDENCE_FALLBACK, level=logging.WARNING, with_utterance=True,
                   intent_confidence=intent_scores.get(suggested_intent or "None", 0.0))
        record_feedback(query.trace_id, user_prompt, "LOW_CONFIDENCE", suggested_intent=suggested_intent)
        return {"message": UNCLEAR_INTENT_MESSAGE}

    intent_confidence = intent_scores.get(child_intent, 0.0)

    if routed_intent == "Alira-MA-DueDiligence" and child_intent == "ScreenTargets":
        with snowflake_cursor(user_context) as cs:
            master_codes = {
                entity: lookup_master_code(cs, category, entities[entity])
                for entity, category in SYNONYM_CATEGORIES.items()
                if entities.get(entity)
            }
            missing = [entity for entity, code in master_codes.items() if code is None]
            for entity in missing:
                query.emit(telemetry.UNMAPPED_TERM, level=logging.WARNING, with_utterance=True,
                           detected_term=redactor.redact(entities[entity]),
                           detected_category=SYNONYM_CATEGORIES[entity],
                           intent_confidence=intent_confidence)
                record_feedback(query.trace_id, user_prompt, "UNMAPPED_TERM", detected_term=entities[entity],
                                detected_category=SYNONYM_CATEGORIES[entity], suggested_intent=child_intent)
            # A filter term with no master code can't match any target, so skip the warehouse query
            if missing:
                query.emit(telemetry.QUERY_ROUTED, row_count=0, outcome="UNMAPPED_TERM",
                           intent_confidence=intent_confidence)
                return []

            base_sql = """
                SELECT company_name, annual_revenue as revenue, therapeutic_area, country
                FROM alira_dw.assistant.m_and_a_targets
                WHERE 1=1
            """
            params = []

            if "TherapeuticArea" in master_codes:
                base_sql += " AND therapeutic_area = %s"
                params.append(master_codes["TherapeuticArea"])

            if "Geography" in master_codes:
                base_sql += " AND country = %s"
                params.append(master_codes["Geography"])

            if entities.get("RevenueFloorUsd"):
                raw_rev = entities["RevenueFloorUsd"].lower().replace("m", "")
                try:
                    numeric_rev = float(raw_rev) * 1_000_000
                    base_sql += " AND annual_revenue >= %s"
                    params.append(numeric_rev)
                except ValueError:
                    pass

            cs.execute(base_sql, tuple(params))
            columns = [desc[0] for desc in cs.description]
            records = [dict(zip(columns, row)) for row in cs.fetchall()]

        query.emit(telemetry.QUERY_ROUTED, row_count=len(records), outcome="ROWS" if records else "NO_ROWS",
                   intent_confidence=intent_confidence)
        # A zero-row answer looks like success to the system and like a broken tool to the consultant
        if not records:
            record_feedback(query.trace_id, user_prompt, "NO_ROWS", suggested_intent=child_intent)
        return records

    query.emit(telemetry.QUERY_ROUTED, row_count=0, outcome="NO_SQL_MAPPING", intent_confidence=intent_confidence)
    return {"message": f"Intent {routed_intent}/{child_intent} caught but no SQL mapping exists."}


def _ask_agent(user_prompt: str, practice: str, query: _QueryTrace) -> dict:
    """Market access and RWE questions go to the Foundry agent, which queries Snowflake through its MCP tools."""
    answer = foundry_orchestrator.ask_orchestrator(user_prompt, trace_id=query.trace_id, user_hash=query.user_hash,
                                                   redacted_prompt=redactor.redact(user_prompt))
    # These practices have no child CLU model, so the tools the agent picked stand in for the intent
    query.intent = "+".join(answer.tools_called) or "None"
    # No automatic feedback row: the steward's approvals train the child CLU model, which has no intents for these
    query.emit(telemetry.QUERY_ROUTED, row_count=len(answer.rows), outcome="AGENT_ROWS" if answer.rows else "AGENT_TEXT")
    return {"type": AGENT_PRACTICES[practice], "message": answer.text, "data": answer.rows}


def _best_candidate(scores: dict):
    """The highest-scoring real intent, i.e. what the model would have picked with a lower threshold."""
    candidates = {name: score for name, score in scores.items() if name != "None"}
    return max(candidates, key=candidates.get) if candidates else None


def lookup_master_code(cs, category: str, user_input: str):
    """Returns the master code a consultant's term maps to, or None when the synonym table has no entry."""
    cs.execute(
        """SELECT master_code FROM alira_dw.assistant.vocabulary_synonyms
           WHERE category=%s AND LOWER(user_input)=LOWER(%s) LIMIT 1""",
        (category, user_input),
    )
    row = cs.fetchone()
    return row[0] if row else None


def pseudonymise_user(user_context: dict = None) -> str:
    """Returns the telemetry user_hash, recording hash -> oid once per process in the identity store."""
    context = user_context or {}
    user_id = context.get("oid") or context.get("preferred_username") or "unknown"
    hashed = telemetry.user_hash(user_id)
    # Hashes from the per-process fallback key are meaningless after a restart, so they aren't worth mapping
    if telemetry.pseudonymisation_key_configured() and hashed not in _registered_user_hashes:
        _registered_user_hashes.add(hashed)
        _background.submit(
            _write_to_warehouse,
            "INSERT INTO alira_dw.telemetry_identity.user_hash_map (user_hash, oid) VALUES (%s, %s)",
            (hashed, user_id),
        )
    return hashed


def record_feedback(trace_id: str, raw_utterance: str, failure_type: str, detected_term: str = None,
                    detected_category: str = None, suggested_intent: str = None) -> None:
    """Queues a row in NLU_FEEDBACK for the data steward. The full utterance is kept here, under DW governance."""
    _background.submit(
        _write_to_warehouse,
        """INSERT INTO alira_dw.assistant.nlu_feedback
           (trace_id, raw_utterance, failure_type, detected_term, detected_category, suggested_intent)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (trace_id, raw_utterance[:1000], failure_type, detected_term and detected_term[:200],
         detected_category, suggested_intent),
    )


def _write_to_warehouse(sql: str, params: tuple) -> None:
    try:
        with snowflake_cursor() as cs:
            cs.execute(sql, params)
    except Exception as exc:
        logger.error("Warehouse write failed: %s", type(exc).__name__)


def load_company_names() -> list[str]:
    """The company-name master list that telemetry redaction strips from utterances."""
    with snowflake_cursor() as cs:
        cs.execute("SELECT company_name FROM alira_dw.assistant.v_company_name_master")
        return [row[0] for row in cs.fetchall()]


redactor = telemetry.UtteranceRedactor(load_company_names)


@contextmanager
def snowflake_cursor(user_context: dict = None):
    tag = f"AliraBot-{user_context.get('preferred_username', 'dev')}" if user_context else "AliraBot-Dev"
    ctx = snowflake.connector.connect(
        user=os.environ.get("SNOWFLAKE_USER"),
        private_key_file=os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE"),
        account=os.environ.get("SNOWFLAKE_ACCOUNT"),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database="ALIRA_DW",
        schema="ASSISTANT",
        session_parameters={'QUERY_TAG': tag}
    )
    try:
        yield ctx.cursor()
    finally:
        ctx.close()
