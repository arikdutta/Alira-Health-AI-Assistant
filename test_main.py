import os
import subprocess
import sys
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    # An empty value keeps load_dotenv from exporting a developer's real connection string during tests
    with patch.dict(os.environ, {"APPLICATIONINSIGHTS_CONNECTION_STRING": ""}):
        import main
        yield TestClient(main.app)


def test_query_returns_the_trace_id_it_logged_under(client):
    with patch("main.query_alira_assistant", return_value=[{"COMPANY_NAME": "Target"}]) as query:
        response = client.post("/query", json={"prompt": "Show me oncology targets in Germany"},
                               headers={"Origin": "http://localhost:5173"})

    assert response.status_code == 200
    trace_id = response.headers["X-Trace-Id"]
    assert query.call_args.kwargs["trace_id"] == trace_id
    # Without this the browser can't read the trace id to send back with a wrong-result report
    assert "X-Trace-Id" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("engine_result, payload", [
    ([{"COMPANY_NAME": "Target"}], {"type": "MA_TARGETS", "message": None, "data": [{"COMPANY_NAME": "Target"}]}),
    ([], {"type": "MA_TARGETS", "message": "No matching targets found.", "data": []}),
    ({"type": "RWE_SEARCH", "message": "Two registries match.", "data": [{"COHORT_NAME": "NSCLC EGFR+"}]},
     {"type": "RWE_SEARCH", "message": "Two registries match.", "data": [{"COHORT_NAME": "NSCLC EGFR+"}]}),
    ({"message": "I couldn't match your request to an Alira practice area."},
     {"type": "GENERIC_MESSAGE", "message": "I couldn't match your request to an Alira practice area.", "data": []}),
])
def test_every_engine_answer_reaches_the_portal_in_one_shape(client, engine_result, payload):
    with patch("main.query_alira_assistant", return_value=engine_result):
        response = client.post("/query", json={"prompt": "Anything"})

    assert response.json() == payload


def test_wrong_result_report_is_queued_for_the_steward(client):
    with patch("main.record_feedback") as record_feedback:
        response = client.post("/feedback", json={"trace_id": "abc123", "prompt": "Show me oncology targets in Germany"})

    assert response.status_code == 202
    record_feedback.assert_called_once_with("abc123", "Show me oncology targets in Germany", "WRONG_RESULT")


def test_wrong_result_report_requires_a_prompt(client):
    with patch("main.record_feedback") as record_feedback:
        response = client.post("/feedback", json={"trace_id": "abc123", "prompt": ""})

    assert response.status_code == 422
    record_feedback.assert_not_called()


def test_telemetry_export_refuses_to_start_without_the_pseudonymisation_secret():
    env = {**os.environ,
           "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=00000000-0000-0000-0000-000000000000",
           "TELEMETRY_HMAC_SECRET": ""}

    result = subprocess.run([sys.executable, "-c", "import main"], env=env, capture_output=True, text=True,
                            cwd=os.path.dirname(os.path.abspath(__file__)), timeout=120)

    assert result.returncode != 0
    assert "TELEMETRY_HMAC_SECRET must be set" in result.stderr
