"""Azure AI Foundry master orchestrator agent, bridged to the Snowflake-managed MCP server.

agent_config.yaml defines the agent. `python foundry_orchestrator.py publish` pushes it to Foundry as a new
agent version, and `ask_orchestrator` sends a consultant query to the latest version. The Snowflake PAT lives
in a Foundry project connection: an MCP `headers` value would be stored in the agent definition, readable by
anyone who can read the agent.

snowflake_engine calls this module for the practices CLU routes to the agent, and owns the consultant's
identity and redaction: callers pass the user hash and redacted prompt in.
"""
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential

CONFIG_PATH = Path(__file__).with_name("agent_config.yaml")
REQUIRED_KEYS = frozenset({"Agent_Instruction", "Model_Deployment", "MCP_Server_Label", "MCP_Server_URL",
                           "MCP_Project_Connection", "Allowed_Tools"})
APPROVAL_MODES = ("never", "always")
ENV_REFERENCE = re.compile(r"\$\{(\w+)\}")
# Snowflake's MCP clients fail on hostnames with underscores, so the account part must use hyphens
SNOWFLAKE_MCP_URL = re.compile(r"https://[^/_]+/api/v2/databases/[^/]+/schemas/[^/]+/mcp-servers/[^/]+")
PROJECT_ENDPOINT = re.compile(r"https://[^/]+/api/projects/[^/]+/?")
ANALYST_CONTENT_KEYS = frozenset({"type", "text", "statement", "confidence", "suggestions"})

_trace_lock = threading.Lock()


@dataclass(frozen=True)
class AgentConfig:
    name: str
    instructions: str
    model_deployment: str
    mcp_server_label: str
    mcp_server_url: str
    mcp_project_connection: str
    allowed_tools: tuple[str, ...]
    approval_mode: str = "never"
    logging: bool = False
    log_path: Path = Path("logs/orchestration_traces.txt")


@dataclass(frozen=True)
class AgentAnswer:
    text: str
    tools_called: tuple[str, ...]
    # Flat records from the tool results, whichever tool produced them
    rows: list[dict]


def load_agent_config(path: Path = CONFIG_PATH) -> AgentConfig:
    with open(path, encoding="utf-8") as config_file:
        document = yaml.safe_load(config_file)
    if not isinstance(document, dict) or len(document) != 1:
        raise ValueError(f"{path.name} must define exactly one agent")
    [(name, settings)] = document.items()

    if "Auth_Token" in settings:
        raise ValueError(f"Remove Auth_Token from {path.name}: store the Snowflake PAT in the Foundry project "
                         "connection named by MCP_Project_Connection instead.")
    missing = sorted(REQUIRED_KEYS - settings.keys())
    if missing:
        raise ValueError(f"{path.name} is missing: {', '.join(missing)}")
    settings = {key: _expand_env(value) for key, value in settings.items()}

    if not SNOWFLAKE_MCP_URL.fullmatch(settings["MCP_Server_URL"]):
        raise ValueError("MCP_Server_URL must look like https://<org>-<account>.snowflakecomputing.com"
                         "/api/v2/databases/<DB>/schemas/<SCHEMA>/mcp-servers/<NAME>")
    approval_mode = settings.get("Approval_Mode", "never")
    if approval_mode not in APPROVAL_MODES:
        raise ValueError(f"Approval_Mode must be one of {APPROVAL_MODES}, not {approval_mode!r}")
    if not settings["Allowed_Tools"]:
        raise ValueError("Allowed_Tools must list at least one tool; an empty list would expose every server tool")

    return AgentConfig(
        name=name,
        instructions=settings["Agent_Instruction"].strip(),
        model_deployment=settings["Model_Deployment"],
        mcp_server_label=settings["MCP_Server_Label"],
        mcp_server_url=settings["MCP_Server_URL"],
        mcp_project_connection=settings["MCP_Project_Connection"],
        allowed_tools=tuple(settings["Allowed_Tools"]),
        approval_mode=approval_mode,
        logging=bool(settings.get("Logging", False)),
        # Relative to the config file, so the log lands in the same place whatever directory the app starts in
        log_path=path.parent / settings.get("Log_Path", AgentConfig.log_path),
    )


def _expand_env(value):
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if not isinstance(value, str):
        return value

    def lookup(match):
        if match.group(1) not in os.environ:
            raise ValueError(f"Environment variable {match.group(1)} is referenced in the agent config but not set")
        return os.environ[match.group(1)]

    return ENV_REFERENCE.sub(lookup, value)


def build_agent_definition(config: AgentConfig) -> PromptAgentDefinition:
    return PromptAgentDefinition(
        model=config.model_deployment,
        instructions=config.instructions,
        tools=[MCPTool(
            server_label=config.mcp_server_label,
            server_url=config.mcp_server_url,
            project_connection_id=config.mcp_project_connection,
            allowed_tools=list(config.allowed_tools),
            require_approval=config.approval_mode,
        )],
    )


@lru_cache(maxsize=1)
def _project_client() -> AIProjectClient:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")
    # The resource's /openai/v1 endpoint serves model deployments only; agents live under a project
    if not PROJECT_ENDPOINT.fullmatch(endpoint):
        raise ValueError("FOUNDRY_PROJECT_ENDPOINT must be the project endpoint, "
                         "https://<resource>.services.ai.azure.com/api/projects/<project>, not the /openai/v1 model endpoint")
    # az login locally; the App Service managed identity in Azure. get_openai_client() then authenticates with a
    # bearer token provider for https://ai.azure.com/.default, so no separate token provider is needed.
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


@lru_cache(maxsize=1)
def _openai_client():
    return _project_client().get_openai_client()


def publish_agent(config: AgentConfig):
    """Creates a new agent version in Foundry. Requests that reference the agent by name use the latest version."""
    return _project_client().agents.create_version(agent_name=config.name, definition=build_agent_definition(config))


def ask_orchestrator(prompt: str, *, trace_id: str, user_hash: str, redacted_prompt: str,
                     config: AgentConfig = None) -> AgentAnswer:
    """Sends a consultant query to the published orchestrator agent."""
    config = config or load_agent_config()
    trace = {"trace_id": trace_id, "user_hash": user_hash, "agent": config.name}
    started = time.perf_counter()
    try:
        response = _openai_client().responses.create(
            input=prompt,
            extra_body={"agent_reference": {"name": config.name, "type": "agent_reference"}},
        )
        trace["response_id"] = response.id
        calls = [item for item in response.output if item.type == "mcp_call"]
        # Tool names and outcomes only: arguments carry the consultant's question and outputs carry warehouse data
        trace["tool_calls"] = [{"server": call.server_label, "tool": call.name, "failed": bool(call.error)} for call in calls]
        # With Approval_Mode "always" Foundry pauses for a human to approve each call, and this service has no approval step
        if any(item.type == "mcp_approval_request" for item in response.output):
            raise RuntimeError(f"Agent {config.name} is waiting for MCP tool approval; "
                               "set Approval_Mode to 'never' for server-side use")
        return AgentAnswer(
            text=response.output_text,
            tools_called=tuple(call.name for call in calls),
            rows=[row for call in calls if not call.error for row in tool_result_rows(call.output)],
        )
    except Exception as exc:
        trace["error_type"] = type(exc).__name__
        raise
    finally:
        if config.logging:
            trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
            trace["redacted_prompt"] = redacted_prompt
            _write_trace(config.log_path, trace)


def tool_result_rows(output) -> list[dict]:
    """Flat records from one Snowflake MCP tool result.

    Handles Cortex Search hits (a list of records, or {"results": [...]}, with nested "@scores"), SQL result sets
    (column metadata plus a row matrix, optionally wrapped in "result_set") and plain lists of records. Anything else, such as the generated SQL and explanation a
    Cortex Analyst tool returns, yields no rows and the agent's text answer stands on its own.
    """
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return []
    # SYSTEM_EXECUTE_SQL wraps the SQL API result set: {"query_id": ..., "result_set": {"resultSetMetaData": ..., "data": ...}}
    if isinstance(parsed, dict) and isinstance(parsed.get("result_set"), dict):
        parsed = parsed["result_set"]
    if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
        parsed = parsed["results"]
    elif isinstance(parsed, dict) and isinstance(parsed.get("data"), list) and "resultSetMetaData" in parsed:
        columns = [column["name"] for column in parsed["resultSetMetaData"]["rowType"]]
        parsed = [dict(zip(columns, row)) for row in parsed["data"]]
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        return []
    # Cortex Analyst replies with content blocks such as [{"text": ...}, {"statement": ..., "confidence": {}}]
    if all(row.keys() <= ANALYST_CONTENT_KEYS for row in parsed):
        return []
    return [_flatten(row) for row in parsed]


def _flatten(row: dict) -> dict:
    # A table cell can't show an object, so {"@scores": {"cosine_similarity": 0.8}} becomes scores_cosine_similarity
    flat = {}
    for key, value in row.items():
        if isinstance(value, dict):
            flat.update({f"{key.lstrip('@')}_{sub_key}": sub_value for sub_key, sub_value in value.items()})
        else:
            flat[key] = value
    return flat


def _write_trace(log_path: Path, trace: dict) -> None:
    line = json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **trace})
    with _trace_lock:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    command, *args = sys.argv[1:] or ["help"]
    if command == "publish":
        agent = publish_agent(load_agent_config())
        print(f"Published {agent.name} version {agent.version}")
    elif command == "ask" and args:
        # Straight to the agent, skipping CLU routing, to smoke-test the agent and its MCP tools
        import telemetry
        from snowflake_engine import pseudonymise_user, redactor

        prompt = " ".join(args)
        answer = ask_orchestrator(prompt, trace_id=telemetry.current_trace_id(),
                                  user_hash=pseudonymise_user({"preferred_username": "cli_user@alira.dev"}),
                                  redacted_prompt=redactor.redact(prompt))
        print(answer.text)
        print(f"\nTools called: {', '.join(answer.tools_called) or 'none'} | rows extracted: {len(answer.rows)}")
    else:
        sys.exit('Usage: python foundry_orchestrator.py publish | ask "<consultant question>"')
