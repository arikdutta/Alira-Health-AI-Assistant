import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from foundry_orchestrator import (CONFIG_PATH, _project_client, ask_orchestrator, build_agent_definition,
                                  load_agent_config, tool_result_rows)

MCP_URL = "https://alira-sandbox.snowflakecomputing.com/api/v2/databases/ALIRA_DW/schemas/ASSISTANT/mcp-servers/ALIRA_MCP"

CONFIG_YAML = """
alira-master-orchestrator-agent:
  Agent_Instruction: |
    Route market access questions to market_access_analyst.
  Model_Deployment: "gpt-5-mini"
  MCP_Server_Label: "alira_multi_agent_mcp"
  MCP_Server_URL: "${SNOWFLAKE_MCP_SERVER_URL}"
  MCP_Project_Connection: "alira-snowflake-mcp"
  Allowed_Tools: ["market_access_analyst", "rwe_registry_search"]
  Approval_Mode: "never"
  Logging: true
  Log_Path: "./logs/orchestration_traces.txt"
"""


SEARCH_OUTPUT = json.dumps({"results": [
    {"COHORT_NAME": "NSCLC EGFR+ registry", "PATIENT_COUNT": 1840, "@scores": {"cosine_similarity": 0.82}},
], "request_id": "req-1"})
SQL_OUTPUT = json.dumps({
    "resultSetMetaData": {"rowType": [{"name": "DRUG_NAME"}, {"name": "COUNTRY"}, {"name": "APPROVED_PRICE_EUR"}]},
    "data": [["Keytruda", "DE", 3100], ["Keytruda", "FR", 2890]],
})
ASK = dict(trace_id="trace-9", user_hash="f" * 64, redacted_prompt="What is [COMPANY]' price in Germany?")


@pytest.fixture(autouse=True)
def mcp_env(monkeypatch):
    monkeypatch.setenv("SNOWFLAKE_MCP_SERVER_URL", MCP_URL)
    monkeypatch.setenv("SNOWFLAKE_MCP_CONNECTION_NAME", "alira-snowflake-mcp")
    monkeypatch.setenv("FOUNDRY_MODEL_DEPLOYMENT_NAME", "gpt-5-mini")


def write_config(tmp_path, text=CONFIG_YAML):
    path = tmp_path / "agent_config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def agent_response(*output, text="Keytruda is reimbursed in Germany at EUR 3,100."):
    return SimpleNamespace(id="resp_1", output=list(output), output_text=text)


def mcp_call(name, output=SQL_OUTPUT, error=None):
    return SimpleNamespace(type="mcp_call", server_label="alira_multi_agent_mcp", name=name,
                           arguments='{"message": "Keytruda Holdings price in Germany"}', output=output, error=error)


def read_traces(config):
    return [json.loads(line) for line in config.log_path.read_text(encoding="utf-8").splitlines()]


def test_shipped_config_loads_from_the_environment():
    config = load_agent_config(CONFIG_PATH)

    assert config.name == "alira-master-orchestrator-agent"
    assert config.model_deployment == "gpt-5-mini"
    assert config.mcp_project_connection == "alira-snowflake-mcp"
    assert config.allowed_tools == ("market_access_analyst", "rwe_registry_search", "run_sql")
    assert "- If they query clinical trial registries" in config.instructions
    assert config.log_path == CONFIG_PATH.parent / "logs" / "orchestration_traces.txt"


def test_definition_attaches_the_snowflake_mcp_tool(tmp_path):
    definition = build_agent_definition(load_agent_config(write_config(tmp_path))).as_dict()

    assert definition["model"] == "gpt-5-mini"
    assert definition["tools"] == [{
        "type": "mcp",
        "server_label": "alira_multi_agent_mcp",
        "server_url": MCP_URL,
        "project_connection_id": "alira-snowflake-mcp",
        "allowed_tools": ["market_access_analyst", "rwe_registry_search"],
        "require_approval": "never",
    }]


@pytest.mark.parametrize("change, error", [
    (('"alira-snowflake-mcp"', '"alira-snowflake-mcp"\n  Auth_Token: "pat-secret"'), "Remove Auth_Token"),
    (('"${SNOWFLAKE_MCP_SERVER_URL}"', '"https://<your_snowflake_account_url>://"'), "MCP_Server_URL must look like"),
    (('"${SNOWFLAKE_MCP_SERVER_URL}"', '"https://org_acct.snowflakecomputing.com/api/v2/databases/D/schemas/S/mcp-servers/M"'),
     "MCP_Server_URL must look like"),
    (('"${SNOWFLAKE_MCP_SERVER_URL}"', '"${UNSET_MCP_URL}"'), "UNSET_MCP_URL"),
    (('["market_access_analyst", "rwe_registry_search"]', "[]"), "Allowed_Tools must list"),
    (('"never"', '"sometimes"'), "Approval_Mode must be one of"),
    (('  Model_Deployment: "gpt-5-mini"\n', ""), "missing: Model_Deployment"),
])
def test_invalid_configs_are_rejected(tmp_path, change, error):
    with pytest.raises(ValueError, match=error):
        load_agent_config(write_config(tmp_path, CONFIG_YAML.replace(*change)))


@pytest.mark.parametrize("endpoint", ["https://alira-heath-project-resource.services.ai.azure.com/openai/v1", ""])
def test_model_endpoint_is_rejected_as_the_project_endpoint(monkeypatch, endpoint):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", endpoint)
    _project_client.cache_clear()

    with pytest.raises(ValueError, match="must be the project endpoint"):
        _project_client()


@patch("foundry_orchestrator._openai_client")
def test_answer_is_returned_and_traced_without_raw_question_or_tool_data(mock_openai, tmp_path):
    config = load_agent_config(write_config(tmp_path))
    mock_openai.return_value.responses.create.return_value = agent_response(
        SimpleNamespace(type="mcp_list_tools"), mcp_call("market_access_analyst"), SimpleNamespace(type="message"))

    answer = ask_orchestrator("What is Keytruda Holdings' price in Germany?", **ASK, config=config)

    assert answer.text == "Keytruda is reimbursed in Germany at EUR 3,100."
    assert answer.tools_called == ("market_access_analyst",)
    assert answer.rows[0] == {"DRUG_NAME": "Keytruda", "COUNTRY": "DE", "APPROVED_PRICE_EUR": 3100}
    mock_openai.return_value.responses.create.assert_called_once_with(
        input="What is Keytruda Holdings' price in Germany?",
        extra_body={"agent_reference": {"name": "alira-master-orchestrator-agent", "type": "agent_reference"}},
    )
    [trace] = read_traces(config)
    assert (trace["trace_id"], trace["user_hash"]) == ("trace-9", "f" * 64)
    assert trace["tool_calls"] == [{"server": "alira_multi_agent_mcp", "tool": "market_access_analyst", "failed": False}]
    assert trace["redacted_prompt"] == "What is [COMPANY]' price in Germany?"
    logged = json.dumps(trace)
    assert "Keytruda" not in logged


@pytest.mark.parametrize("output, rows", [
    (SEARCH_OUTPUT, [{"COHORT_NAME": "NSCLC EGFR+ registry", "PATIENT_COUNT": 1840, "scores_cosine_similarity": 0.82}]),
    (SQL_OUTPUT, [{"DRUG_NAME": "Keytruda", "COUNTRY": "DE", "APPROVED_PRICE_EUR": 3100},
                  {"DRUG_NAME": "Keytruda", "COUNTRY": "FR", "APPROVED_PRICE_EUR": 2890}]),
    ('[{"DRUG_NAME": "Keytruda"}]', [{"DRUG_NAME": "Keytruda"}]),
    ("SELECT approved_price_eur FROM market_access_reimbursement WHERE drug_name = 'Keytruda'", []),
    ('{"text": "Here is the SQL", "sql": "SELECT 1"}', []),
    # The shapes the Snowflake MCP tools actually return: Cortex Analyst, Cortex Search and SYSTEM_EXECUTE_SQL
    ('[{"text": "This is our interpretation of your question"}, {"statement": "SELECT 1;", "confidence": {}}]', []),
    ('[{"country": "DE", "@scores": {"cosine_similarity": 0.49}, "cohort_name": "Non-Small Cell Lung Cancer"}]',
     [{"country": "DE", "scores_cosine_similarity": 0.49, "cohort_name": "Non-Small Cell Lung Cancer"}]),
    (json.dumps({"query_id": "q-1", "result_set": {
        "resultSetMetaData": {"numRows": 1, "rowType": [{"name": "DRUG_NAME"}, {"name": "TOTAL_APPROVED_PRICE_EUR"}]},
        "data": [["Keytruda", "4200.00"]], "statementHandle": "q-1"}}),
     [{"DRUG_NAME": "Keytruda", "TOTAL_APPROVED_PRICE_EUR": "4200.00"}]),
    (None, []),
])
def test_tool_results_become_flat_rows(output, rows):
    assert tool_result_rows(output) == rows


@patch("foundry_orchestrator._openai_client")
def test_failed_tool_calls_contribute_no_rows(mock_openai, tmp_path):
    config = load_agent_config(write_config(tmp_path))
    mock_openai.return_value.responses.create.return_value = agent_response(
        mcp_call("rwe_registry_search", output=SEARCH_OUTPUT, error="Search service unavailable"),
        mcp_call("market_access_analyst"))

    answer = ask_orchestrator("Keytruda prices and NSCLC registries", **ASK, config=config)

    assert answer.tools_called == ("rwe_registry_search", "market_access_analyst")
    assert [row["COUNTRY"] for row in answer.rows] == ["DE", "FR"]
    assert read_traces(config)[0]["tool_calls"][0]["failed"] is True


@patch("foundry_orchestrator._openai_client")
def test_pending_tool_approval_fails_instead_of_returning_an_empty_answer(mock_openai, tmp_path):
    config = load_agent_config(write_config(tmp_path, CONFIG_YAML.replace('"never"', '"always"')))
    mock_openai.return_value.responses.create.return_value = agent_response(
        SimpleNamespace(type="mcp_approval_request", id="apr_1"), text="")

    with pytest.raises(RuntimeError, match="waiting for MCP tool approval"):
        ask_orchestrator("Patient counts for NSCLC registries", **ASK, config=config)

    [trace] = read_traces(config)
    assert trace["error_type"] == "RuntimeError"


@patch("foundry_orchestrator._openai_client")
def test_no_trace_file_when_logging_is_off(mock_openai, tmp_path):
    config = load_agent_config(write_config(tmp_path, CONFIG_YAML.replace("Logging: true", "Logging: false")))
    mock_openai.return_value.responses.create.return_value = agent_response(mcp_call("rwe_registry_search"))

    ask_orchestrator("Patient counts for NSCLC registries", **ASK, config=config)

    assert not config.log_path.exists()
