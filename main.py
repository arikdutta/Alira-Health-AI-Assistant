import os
from contextlib import asynccontextmanager
from azure.monitor.opentelemetry import configure_azure_monitor
from dotenv import load_dotenv

# Load .env before importing snowflake_engine and teams_bot, which read env vars at import/construction time
load_dotenv()

# Send logs, request traces and dependency calls to Application Insights. Azure App Service injects the
# connection string at runtime; locally and in tests it's unset, and the exporter raises on an empty one.
# This must run before FastAPI is imported: instrumentation swaps in a traced fastapi.FastAPI class.
if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
    # Fail closed: without a stable secret, user_hash can't be reversed by the identity store when needed
    if not os.environ.get("TELEMETRY_HMAC_SECRET"):
        raise RuntimeError("TELEMETRY_HMAC_SECRET must be set before telemetry is exported to Application Insights.")
    configure_azure_monitor(
        connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"]
    )

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from snowflake_engine import query_alira_assistant, record_feedback, redactor
from telemetry import current_trace_id
from teams_bot import create_teams_app

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Until the company master list loads, utterances are withheld from telemetry, so start loading it now
    redactor.refresh_in_background()
    # Registers POST /api/messages, with Bot Framework JWT validation when CLIENT_ID is set
    await teams_app.initialize()
    yield

app = FastAPI(title="Alira Health Assistant API Gateway", lifespan=lifespan)
teams_app = create_teams_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the browser read the trace id it sends back with a wrong-result report
    expose_headers=["X-Trace-Id"],
)

class QueryRequest(BaseModel):
    prompt: str

class WrongResultReport(BaseModel):
    trace_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=1000)

def portal_payload(result) -> dict:
    """The one shape the web portal renders, whichever engine answered: {type, message, data}.

    type is MA_TARGETS (CLU + warehouse rows), MARKET_ACCESS or RWE_SEARCH (Foundry agent text plus any rows its
    tools returned), or GENERIC_MESSAGE (the engine couldn't answer).
    """
    if isinstance(result, list):
        return {"type": "MA_TARGETS", "message": None if result else "No matching targets found.", "data": result}
    return {"type": result.get("type", "GENERIC_MESSAGE"), "message": result.get("message"), "data": result.get("data", [])}

@app.get("/")
def health():
    return {"status": "online"}

@app.post("/query")
async def execute_query(request: QueryRequest, response: Response):
    if not request.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt empty.")
    trace_id = current_trace_id()
    response.headers["X-Trace-Id"] = trace_id
    try:
        mock_user = {"preferred_username": "sandbox_user@alira.dev"}
        return portal_payload(query_alira_assistant(request.prompt, mock_user, trace_id=trace_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e), headers={"X-Trace-Id": trace_id})

@app.post("/feedback", status_code=202)
def report_wrong_result(report: WrongResultReport):
    """A consultant flags an answer as wrong; the data steward reviews it in the NLU_FEEDBACK app."""
    record_feedback(report.trace_id, report.prompt, "WRONG_RESULT")
    return {"status": "received"}
