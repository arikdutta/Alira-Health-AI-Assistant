import copy
import logging
import pytest
from unittest.mock import ANY, patch, MagicMock
import snowflake_engine
import telemetry
from foundry_orchestrator import AgentAnswer
from snowflake_engine import query_alira_assistant

CONSULTANT = {"preferred_username": "consultant@alira.dev"}

MOCK_CLU_RESPONSE = {
    "result": {
        "prediction": {
            "topIntent": "Alira-MA-DueDiligence",
            "projectKind": "Orchestration",
            "intents": {
                "Alira-MA-DueDiligence": {
                    "targetProjectKind": "Conversation",
                    "confidenceScore": 0.92,
                    "result": {
                        "prediction": {
                            "topIntent": "ScreenTargets",
                            "projectKind": "Conversation",
                            "intents": [
                                {"category": "ScreenTargets", "confidenceScore": 0.88},
                                {"category": "GetDealComparables", "confidenceScore": 0.07}
                            ],
                            "entities": [
                                {"category": "TherapeuticArea", "text": "cancer"},
                                {"category": "Geography", "text": "germany"},
                                {"category": "RevenueFloorUsd", "text": "20M"}
                            ]
                        }
                    }
                },
                "None": {"targetProjectKind": "NonLinked", "confidenceScore": 0.05}
            }
        }
    }
}

MOCK_CLU_NONE_RESPONSE = {
    "result": {
        "prediction": {
            "topIntent": "None",
            "projectKind": "Orchestration",
            "intents": {
                "None": {"targetProjectKind": "NonLinked", "confidenceScore": 0.6},
                "Alira-MA-DueDiligence": {"targetProjectKind": "Conversation", "confidenceScore": 0.31}
            }
        }
    }
}

# Snowflake cursors return rows as tuples, ordered like cursor.description
MOCK_SNOWFLAKE_DATA = [
    ("BioTech DE", 25000000, "Oncology", "DE")
]

@pytest.fixture(autouse=True)
def no_warehouse_writes():
    # Feedback and identity-map writes run on a background executor; tests assert on record_feedback instead
    with patch("snowflake_engine._background"):
        snowflake_engine.redactor.set_names(["BioTech DE"])
        yield

@pytest.fixture
def feedback():
    with patch("snowflake_engine.record_feedback") as record_feedback:
        yield record_feedback

@pytest.fixture
def mock_cursor():
    with patch('snowflake_engine.snowflake.connector.connect') as mock_snowflake_connect:
        cursor = MagicMock()
        cursor.fetchall.return_value = MOCK_SNOWFLAKE_DATA
        cursor.description = [("COMPANY_NAME",), ("REVENUE",), ("THERAPEUTIC_AREA",), ("COUNTRY",)]
        mock_snowflake_connect.return_value.cursor.return_value = cursor
        yield cursor

def events(caplog, name):
    records = [r for r in caplog.records if getattr(r, telemetry.CUSTOM_EVENT_NAME_ATTRIBUTE, None) == name]
    for record in records:
        # Privacy rules: pseudonymised user, no raw identity or company name anywhere in the event
        assert record.user_hash == telemetry.user_hash("consultant@alira.dev")
        values = " ".join(str(value) for value in vars(record).values())
        assert "consultant@alira.dev" not in values
        assert "BioTech DE" not in values
    return records

def clu_with_child(**child_changes):
    response = copy.deepcopy(MOCK_CLU_RESPONSE)
    response["result"]["prediction"]["intents"]["Alira-MA-DueDiligence"]["result"]["prediction"].update(child_changes)
    return response

@patch('snowflake_engine.language_client.analyze_conversation', return_value=MOCK_CLU_RESPONSE)
def test_successful_ma_screening_pipeline(_, mock_cursor, feedback, caplog):
    # Synonym lookups for "cancer" then "germany"
    mock_cursor.fetchone.side_effect = [("Oncology",), ("DE",)]

    with caplog.at_level(logging.INFO):
        results = query_alira_assistant("Show me oncology targets in Germany with revenue over 20M", CONSULTANT, trace_id="trace-1")

    assert len(results) == 1
    assert results[0]["COMPANY_NAME"] == "BioTech DE"
    assert results[0]["REVENUE"] == 25000000
    main_sql, main_params = mock_cursor.execute.call_args_list[-1].args
    assert "m_and_a_targets" in main_sql
    assert main_params == ("Oncology", "DE", 20_000_000.0)

    [routed] = events(caplog, "QueryRouted")
    assert routed.levelno == logging.INFO
    assert (routed.trace_id, routed.practice, routed.intent) == ("trace-1", "Alira-MA-DueDiligence", "ScreenTargets")
    assert (routed.confidence, routed.intent_confidence) == (0.92, 0.88)
    assert (routed.row_count, routed.outcome) == (1, "ROWS")
    assert isinstance(routed.latency_ms, int)
    assert not events(caplog, "UnmappedTerm")
    feedback.assert_not_called()

@patch('snowflake_engine.language_client.analyze_conversation', return_value=MOCK_CLU_RESPONSE)
def test_missing_synonym_is_logged_and_skips_target_query(_, mock_cursor, feedback, caplog):
    # "cancer" has no synonym row; "germany" maps to DE
    mock_cursor.fetchone.side_effect = [None, ("DE",)]

    with caplog.at_level(logging.INFO):
        results = query_alira_assistant("Show me cancer targets in Germany over 20M", CONSULTANT, trace_id="trace-2")

    assert results == []
    assert all("m_and_a_targets" not in c.args[0] for c in mock_cursor.execute.call_args_list)

    [unmapped] = events(caplog, "UnmappedTerm")
    assert unmapped.levelno == logging.WARNING
    assert (unmapped.detected_term, unmapped.detected_category) == ("cancer", "THERAPEUTIC_AREA")
    assert unmapped.redacted_utterance == "Show me cancer targets in Germany over [NUM]"
    [routed] = events(caplog, "QueryRouted")
    assert (routed.row_count, routed.outcome) == (0, "UNMAPPED_TERM")
    # The steward gets the full utterance in Snowflake, not the redacted one
    feedback.assert_called_once_with("trace-2", "Show me cancer targets in Germany over 20M", "UNMAPPED_TERM",
                                     detected_term="cancer", detected_category="THERAPEUTIC_AREA",
                                     suggested_intent="ScreenTargets")

@patch('snowflake_engine.language_client.analyze_conversation', return_value=MOCK_CLU_RESPONSE)
def test_zero_row_answer_is_flagged_for_review(_, mock_cursor, feedback, caplog):
    mock_cursor.fetchone.side_effect = [("Oncology",), ("DE",)]
    mock_cursor.fetchall.return_value = []

    with caplog.at_level(logging.INFO):
        results = query_alira_assistant("Show me oncology targets in Germany", CONSULTANT, trace_id="trace-3")

    assert results == []
    [routed] = events(caplog, "QueryRouted")
    assert (routed.row_count, routed.outcome) == (0, "NO_ROWS")
    feedback.assert_called_once_with("trace-3", "Show me oncology targets in Germany", "NO_ROWS",
                                     suggested_intent="ScreenTargets")

@patch('snowflake_engine.language_client.analyze_conversation')
def test_unrecognized_intent_triggers_clu_fallback(mock_azure_clu, mock_cursor, feedback, caplog):
    mock_azure_clu.return_value = copy.deepcopy(MOCK_CLU_NONE_RESPONSE)

    with caplog.at_level(logging.INFO):
        result = query_alira_assistant("What's the weather in Berlin?", CONSULTANT, trace_id="trace-4")

    assert result == {"message": "I couldn't match your request to an Alira practice area."}
    mock_cursor.execute.assert_not_called()

    [fallback] = events(caplog, "LowConfidenceFallback")
    assert fallback.levelno == logging.WARNING
    assert (fallback.practice, fallback.intent, fallback.row_count) == ("None", "None", 0)
    # The best practice the orchestrator considered, and how close it came
    assert (fallback.candidate_practice, fallback.confidence) == ("Alira-MA-DueDiligence", 0.31)
    assert fallback.redacted_utterance == "What's the weather in Berlin?"
    assert not events(caplog, "QueryRouted")
    feedback.assert_called_once_with("trace-4", "What's the weather in Berlin?", "LOW_CONFIDENCE")

@patch('snowflake_engine.language_client.analyze_conversation')
def test_unclear_child_intent_triggers_fallback_with_suggested_intent(mock_azure_clu, mock_cursor, feedback, caplog):
    mock_azure_clu.return_value = clu_with_child(topIntent="None", intents=[
        {"category": "None", "confidenceScore": 0.55},
        {"category": "GetDealComparables", "confidenceScore": 0.4},
        {"category": "ScreenTargets", "confidenceScore": 0.05},
    ])

    with caplog.at_level(logging.INFO):
        result = query_alira_assistant("multiples for BioTech DE", CONSULTANT, trace_id="trace-5")

    assert result == {"message": snowflake_engine.UNCLEAR_INTENT_MESSAGE}
    mock_cursor.execute.assert_not_called()
    [fallback] = events(caplog, "LowConfidenceFallback")
    assert (fallback.practice, fallback.intent, fallback.intent_confidence) == ("Alira-MA-DueDiligence", "None", 0.4)
    assert fallback.redacted_utterance == "multiples for [COMPANY]"
    feedback.assert_called_once_with("trace-5", "multiples for BioTech DE", "LOW_CONFIDENCE",
                                     suggested_intent="GetDealComparables")

@pytest.mark.parametrize("practice, payload_type", [("Alira-MarketAccess", "MARKET_ACCESS"), ("Alira-RWE", "RWE_SEARCH")])
@patch('snowflake_engine.foundry_orchestrator.ask_orchestrator')
@patch('snowflake_engine.language_client.analyze_conversation')
def test_agent_practices_are_answered_by_the_foundry_agent(mock_azure_clu, ask_orchestrator, mock_cursor, feedback, caplog,
                                                          practice, payload_type):
    # Practices with no child project come back from the orchestrator without a nested child prediction
    mock_azure_clu.return_value = {"result": {"prediction": {"topIntent": practice, "projectKind": "Orchestration", "intents": {
        practice: {"targetProjectKind": "NonLinked", "confidenceScore": 0.81},
        "Alira-MA-DueDiligence": {"targetProjectKind": "Conversation", "confidenceScore": 0.12},
    }}}}
    rows = [{"DRUG_NAME": "BioTech DE oncolytic", "APPROVED_PRICE_EUR": 3100}]
    ask_orchestrator.return_value = AgentAnswer(text="Approved at EUR 3,100 in Germany.",
                                                tools_called=("market_access_analyst", "run_sql"), rows=rows)

    with caplog.at_level(logging.INFO):
        result = query_alira_assistant("What does BioTech DE charge in Germany?", CONSULTANT, trace_id="trace-8")

    assert result == {"type": payload_type, "message": "Approved at EUR 3,100 in Germany.", "data": rows}
    ask_orchestrator.assert_called_once_with("What does BioTech DE charge in Germany?", trace_id="trace-8",
                                             user_hash=telemetry.user_hash("consultant@alira.dev"),
                                             redacted_prompt="What does [COMPANY] charge in Germany?")
    mock_cursor.execute.assert_not_called()
    [routed] = events(caplog, "QueryRouted")
    assert (routed.practice, routed.intent, routed.confidence) == (practice, "market_access_analyst+run_sql", 0.81)
    assert (routed.row_count, routed.outcome) == (1, "AGENT_ROWS")
    feedback.assert_not_called()

@patch('snowflake_engine.language_client.analyze_conversation', side_effect=TimeoutError("CLU timed out"))
def test_failure_emits_query_failed_and_reraises(_, mock_cursor, feedback, caplog):
    with caplog.at_level(logging.INFO), pytest.raises(TimeoutError):
        query_alira_assistant("Is BioTech DE worth more than 50M?", CONSULTANT, trace_id="trace-6")

    [failed] = events(caplog, "QueryFailed")
    assert failed.levelno == logging.ERROR
    assert (failed.error_type, failed.trace_id, failed.practice) == ("TimeoutError", "trace-6", "")
    assert failed.redacted_utterance == "Is [COMPANY] worth more than [NUM]?"
    assert "CLU timed out" not in " ".join(str(value) for value in vars(failed).values())
    feedback.assert_not_called()

def test_user_ids_are_mapped_once_in_the_identity_store(monkeypatch):
    monkeypatch.setenv("TELEMETRY_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(snowflake_engine, "_registered_user_hashes", set())

    first = snowflake_engine.pseudonymise_user({"preferred_username": "aad-oid-1"})
    second = snowflake_engine.pseudonymise_user({"preferred_username": "aad-oid-1"})

    assert first == second == telemetry.user_hash("aad-oid-1")
    snowflake_engine._background.submit.assert_called_once_with(ANY, ANY, (first, "aad-oid-1"))
    assert "telemetry_identity.user_hash_map" in snowflake_engine._background.submit.call_args.args[1]

def test_record_feedback_truncates_to_the_column_sizes():
    snowflake_engine.record_feedback("trace-7", "x" * 1500, "UNMAPPED_TERM", detected_term="y" * 300)

    _, sql, params = snowflake_engine._background.submit.call_args.args
    assert "nlu_feedback" in sql
    assert (len(params[1]), len(params[3])) == (1000, 200)
