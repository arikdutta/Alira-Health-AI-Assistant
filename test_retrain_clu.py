import copy
import json

import pytest

from scripts import retrain_clu
from scripts.retrain_clu import (BASE_DATASET, Config, build_project_payload, build_vocabulary, check_quality_gate,
                                 label_entities, prune_trained_models, run_smoke_tests)

BASE = json.loads(BASE_DATASET.read_text(encoding="utf-8"))


def evaluation(macro_f1, macro_precision):
    return {"projectKind": "Conversation",
            "intentsEvaluation": {"macroF1": macro_f1, "macroPrecision": macro_precision, "macroRecall": 0.9},
            "entitiesEvaluation": {"macroF1": 0.9}}


class FakeProject:
    name = "alira-transaction-advisory"

    def __init__(self, models=(), deployments=(), predictions=None):
        self.responses = {"/models": {"value": list(models)}, "/deployments": {"value": list(deployments)}}
        self.predictions = predictions or {}
        self.deleted = []

    def get(self, path):
        return self.responses[path]

    def request(self, method, path, body=None):
        assert method == "DELETE"
        self.deleted.append(path)

    def predict(self, text, deployment):
        return self.predictions[text]


def test_labeler_reproduces_every_seed_label():
    # If the auto-labeller agrees with the hand-labelled seed set, approved utterances get consistent labels
    vocabulary = build_vocabulary(BASE, synonyms=[])

    for utterance in BASE["assets"]["utterances"]:
        expected = sorted(utterance["entities"], key=lambda label: label["offset"])
        assert label_entities(utterance["text"], vocabulary) == expected, utterance["text"]


def test_labeler_prefers_the_longest_term_and_counts_utf16_offsets():
    vocabulary = {"TherapeuticArea": {"tumors", "solid tumors"}, "Geography": {"italy"}}

    assert label_entities("🚀 solid tumors in Italy", vocabulary) == [
        {"category": "TherapeuticArea", "offset": 3, "length": 12},
        {"category": "Geography", "offset": 19, "length": 5},
    ]


def test_payload_merges_approved_utterances_and_skips_unusable_ones(capsys):
    base_before = copy.deepcopy(BASE)
    approved = [
        ("Show me cancer targets in Italy with revenue over 5M", "ScreenTargets"),
        ("show me ONCOLOGY targets in germany   with revenue over 20M", "ScreenTargets"),  # seed duplicate
        ("Book me a flight to Rome", "BookTravel"),                                      # unknown intent
        ("", "ScreenTargets"),
        ("x" * 501, "None"),                                                             # over authoring limit
    ]
    synonyms = [("THERAPEUTIC_AREA", "cancer"), ("GEOGRAPHY", "italy"), ("SOMETHING_ELSE", "ignored")]

    payload = build_project_payload(BASE, approved, synonyms, "alira-transaction-advisory-ci")

    assert BASE == base_before
    assert payload["metadata"]["projectName"] == "alira-transaction-advisory-ci"
    utterances = payload["assets"]["utterances"]
    assert len(utterances) == len(BASE["assets"]["utterances"]) + 1
    assert utterances[-1] == {
        "text": "Show me cancer targets in Italy with revenue over 5M",
        "language": "en-us",
        "intent": "ScreenTargets",
        "entities": [
            {"category": "TherapeuticArea", "offset": 8, "length": 6},
            {"category": "Geography", "offset": 26, "length": 5},
            {"category": "RevenueFloorUsd", "offset": 50, "length": 2},
        ],
    }
    output = capsys.readouterr().out
    assert "Merged 1 approved utterances" in output and "4 skipped" in output
    # Approved utterances can name confidential targets, so CI logs get counts only
    assert "Italy" not in output


def test_healthy_model_passes_the_gate(capsys):
    check_quality_gate(evaluation(0.91, 0.88))
    assert "macro F1 0.910 | macro precision 0.880" in capsys.readouterr().out


@pytest.mark.parametrize("payload, reason", [
    (evaluation(0.62, 0.90), "F1 0.620 / precision 0.900 below thresholds"),         # synthetic bad-data commit
    (evaluation(0.90, 0.71), "F1 0.900 / precision 0.710 below thresholds"),
    ({"projectKind": "Conversation", "evaluationOptions": {}}, "no intentsEvaluation"),
    (evaluation(91.0, 88.0), "not a score between 0 and 1"),
    (evaluation(None, 0.9), "macro F1 is None"),
])
def test_gate_blocks_deployment(payload, reason):
    with pytest.raises(SystemExit, match=reason):
        check_quality_gate(payload)


def test_gate_reads_the_nested_summary_shape():
    nested = {"customConversationalSummary": evaluation(0.9, 0.85)}
    check_quality_gate(nested)


def test_pruning_keeps_deployed_and_newest_models():
    models = [{"label": f"v{week}", "modelId": f"id-{week}", "lastTrainedDateTime": f"2026-07-{week:02d}T02:00:00Z"}
              for week in range(1, 10)]
    deployments = [{"deploymentName": "production-v1", "modelId": "id-1"},
                   {"deploymentName": "staging", "modelId": "id-9"}]
    project = FakeProject(models, deployments)

    prune_trained_models(project, keep=5)

    # Undeployed newest-first: v8 v7 v6 v5 v4 kept, v3 and v2 deleted; v1 and v9 are deployed
    assert project.deleted == ["/models/v3", "/models/v2"]


def test_pruning_does_nothing_when_deployed_models_are_unidentifiable():
    project = FakeProject([{"label": f"v{i}", "modelId": f"id-{i}"} for i in range(9)],
                          [{"deploymentName": "production-v1"}])

    prune_trained_models(project, keep=1)

    assert project.deleted == []


def test_smoke_tests_block_promotion_on_a_regression(tmp_path, monkeypatch):
    regression_set = tmp_path / "regression.json"
    regression_set.write_text(json.dumps({"cases": [
        {"text": "Find cancer assets in France", "practice": "Alira-MA-DueDiligence", "intent": "ScreenTargets",
         "entities": {"Geography": "France"}},
        {"text": "Reset my laptop password", "practice": "None"},
    ]}))
    monkeypatch.setattr(retrain_clu, "REGRESSION_SET", regression_set)
    project = FakeProject(predictions={
        "Find cancer assets in France": {"topIntent": "ScreenTargets", "entities": [{"category": "Geography", "text": "Paris"}]},
    })

    with pytest.raises(SystemExit, match="Geography 'paris', expected 'France'"):
        run_smoke_tests(project, "staging", "intent")


def test_config_defaults_to_a_gate_only_run_against_the_orchestrators_slot(monkeypatch):
    monkeypatch.setenv("AZURE_LANGUAGE_ENDPOINT", "https://lang.example.com/")
    monkeypatch.setenv("AZURE_LANGUAGE_KEY", "key")
    for name in ("DEPLOY", "PROJECT", "PRODUCTION_DEPLOYMENT", "TRAINING_CONFIG_VERSION"):
        monkeypatch.delenv(name, raising=False)

    config = Config.from_env()

    assert config.deploy is False
    assert config.endpoint == "https://lang.example.com"
    # clu_orchestrator_dataset.json routes to this deployment of the child project
    orchestrator = json.loads((BASE_DATASET.parent / "clu_orchestrator_dataset.json").read_text(encoding="utf-8"))
    routed = orchestrator["assets"]["intents"][0]["orchestration"]["conversationOrchestration"]
    assert (config.project, config.production_deployment) == (routed["projectName"], routed["deploymentName"])
