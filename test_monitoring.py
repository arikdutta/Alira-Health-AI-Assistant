import json
import re
from pathlib import Path

import pytest

import telemetry

MONITORING = Path(__file__).parent / "monitoring"
KQL_FILES = sorted((MONITORING / "kql").glob("*.kql"))

# Every dimension snowflake_engine sends: the seven shared ones plus the per-event extras
EMITTED_DIMENSIONS = {"trace_id", "practice", "intent", "confidence", "latency_ms", "row_count", "user_hash",
                      "outcome", "intent_confidence", "candidate_practice", "detected_term", "detected_category",
                      "redacted_utterance", "error_type"}
EVENT_NAMES = {telemetry.QUERY_ROUTED, telemetry.LOW_CONFIDENCE_FALLBACK, telemetry.UNMAPPED_TERM, telemetry.QUERY_FAILED}


def query_body(kql: str) -> str:
    return "\n".join(line for line in kql.splitlines()
                     if line.strip() and not line.strip().startswith(("//", "| render")))


@pytest.mark.parametrize("kql_file", KQL_FILES, ids=lambda path: path.name)
def test_dashboards_only_read_events_and_dimensions_the_api_emits(kql_file):
    kql = kql_file.read_text(encoding="utf-8")

    assert set(re.findall(r'name == "(\w+)"', kql)) <= EVENT_NAMES
    assert set(re.findall(r"customDimensions\.(\w+)", kql)) <= EMITTED_DIMENSIONS


def test_workbook_matches_the_kql_files():
    workbook = json.loads((MONITORING / "alira_nlu_workbook.json").read_text(encoding="utf-8"))
    workbook_queries = [item["content"]["query"] for item in workbook["items"] if item["type"] == 3]

    assert workbook_queries == [query_body(path.read_text(encoding="utf-8")) for path in KQL_FILES]
