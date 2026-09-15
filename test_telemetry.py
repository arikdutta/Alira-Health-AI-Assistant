import logging

import pytest
from azure.monitor.opentelemetry.exporter.export.logs._exporter import _convert_log_to_envelope
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

import telemetry
from telemetry import UtteranceRedactor

EVENT_DIMENSIONS = dict(trace_id="abc123", practice="Alira-MA-DueDiligence", intent="ScreenTargets",
                        confidence=0.912345, latency_ms=840, row_count=0, user_hash="f" * 64)


def loaded_redactor(names):
    redactor = UtteranceRedactor(load_names=lambda: pytest.fail("should not reload"))
    redactor.set_names(names)
    return redactor


def test_user_hash_is_a_keyed_hmac_not_the_id(monkeypatch):
    monkeypatch.setenv("TELEMETRY_HMAC_SECRET", "secret-one")
    first = telemetry.user_hash("00000000-aaaa-bbbb-cccc-000000000001")

    assert first == telemetry.user_hash("00000000-aaaa-bbbb-cccc-000000000001")
    assert len(first) == 64 and "aaaa" not in first
    monkeypatch.setenv("TELEMETRY_HMAC_SECRET", "secret-two")
    assert telemetry.user_hash("00000000-aaaa-bbbb-cccc-000000000001") != first


def test_redactor_withholds_utterances_until_the_master_list_loads():
    def unavailable():
        raise ConnectionError("warehouse down")

    redactor = UtteranceRedactor(load_names=unavailable)

    assert redactor.redact("Is Kura Therapeutics DE still for sale?") == telemetry.WITHHELD


@pytest.mark.parametrize("utterance, expected", [
    ("Is Kura Therapeutics DE still for sale?", "Is [COMPANY] still for sale?"),
    ("is kura   therapeutics de still for sale?", "is [COMPANY] still for sale?"),
    ("Compare Kura Therapeutics DE with Kura", "Compare [COMPANY] with [COMPANY]"),
    ("Kurama assets in Germany", "Kurama assets in Germany"),
    ("oncology targets with revenue over 20M", "oncology targets with revenue over [NUM]"),
    ("deals above $1.5bn since 2023", "deals above [NUM] since [NUM]"),
    ("EBITDA margin over 12% and 3 million users", "EBITDA margin over [NUM] and [NUM] users"),
])
def test_redactor_strips_company_names_and_figures(utterance, expected):
    assert loaded_redactor(["Kura Therapeutics DE", "Kura"]).redact(utterance) == expected


def test_emit_event_refuses_identifying_dimensions():
    with pytest.raises(ValueError, match="RawUtterance"):
        telemetry.emit_event(telemetry.QUERY_ROUTED, **EVENT_DIMENSIONS, RawUtterance="Is Kura for sale?")


def test_events_are_exported_to_custom_events_with_flat_dimensions():
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    handler = LoggingHandler(logger_provider=provider)
    telemetry.logger.addHandler(handler)
    try:
        telemetry.emit_event(telemetry.QUERY_ROUTED, **EVENT_DIMENSIONS, outcome="NO_ROWS", unused=None)
    finally:
        telemetry.logger.removeHandler(handler)

    [record] = exporter.get_finished_logs()
    envelope = _convert_log_to_envelope(record)

    assert envelope.data.base_type == "EventData"
    assert envelope.data.base_data.name == "QueryRouted"
    dimensions = envelope.data.base_data.properties
    # The KQL dashboards cast these strings with toint() / todouble()
    assert dimensions["row_count"] == "0"
    assert dimensions["confidence"] == "0.9123"
    assert dimensions["outcome"] == "NO_ROWS"
    assert {"trace_id", "practice", "intent", "latency_ms", "user_hash"} <= dimensions.keys()
    assert "unused" not in dimensions
    assert telemetry.CUSTOM_EVENT_NAME_ATTRIBUTE not in dimensions


def test_emit_event_logs_at_the_requested_level(caplog):
    with caplog.at_level(logging.INFO, logger="AliraAssistantLogger"):
        telemetry.emit_event(telemetry.QUERY_FAILED, **EVENT_DIMENSIONS, level=logging.ERROR, error_type="TimeoutError")

    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert getattr(record, telemetry.CUSTOM_EVENT_NAME_ATTRIBUTE) == "QueryFailed"
    assert record.error_type == "TimeoutError"
