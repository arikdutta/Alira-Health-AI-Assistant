"""Weekly CLU retraining with a quality gate (Phase 5.5, Variant A).

1. Merge the checked-in seed dataset with steward-approved feedback from V_APPROVED_TRAINING_DATA and import it.
2. Train, then block deployment when intent macro F1 / macro precision fall below the thresholds.
3. Deploy to the staging slot and replay the regression set against it.
4. When DEPLOY=true: promote to the production slot, then retrain the orchestrator the same way.

DEPLOY defaults to false, so a manual or branch run stops after the gate and the staging smoke test.

Usage: python scripts/retrain_clu.py
"""
import copy
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import snowflake.connector

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_DATASET = REPO_ROOT / "clu_base_dataset.json"
REGRESSION_SET = REPO_ROOT / "clu_regression_set.json"
ORCHESTRATOR_PROJECT = "Alira-Master-Orchestrator"

MIN_MACRO_F1, MIN_MACRO_PRECISION = 0.85, 0.80
# CLU allows at most 10 trained models per project; a weekly run would hit that cap in week 11
KEEP_UNDEPLOYED_MODELS = 5
MAX_AUTHORING_UTTERANCE_CHARS = 500

# CLU entity learned from each vocabulary_synonyms category
SYNONYM_ENTITIES = {"THERAPEUTIC_AREA": "TherapeuticArea", "GEOGRAPHY": "Geography"}
# Entities recognised by their shape rather than a vocabulary; group 1 is the labelled span
PATTERN_ENTITIES = {
    "RevenueFloorUsd": re.compile(
        r"\b(?:over|above|more than|at least|exceeding|greater than)\s+\$?(\d+(?:\.\d+)?\s?m)\b", re.IGNORECASE
    ),
    "YearFrom": re.compile(r"\b(?:since|from|after)\s+((?:19|20)\d{2})\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class Config:
    endpoint: str
    key: str
    project: str
    api_version: str
    training_config_version: str
    staging_deployment: str
    production_deployment: str
    deploy: bool

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            endpoint=os.environ["AZURE_LANGUAGE_ENDPOINT"].rstrip("/"),
            key=os.environ["AZURE_LANGUAGE_KEY"],
            project=os.environ.get("PROJECT", "alira-transaction-advisory"),
            api_version=os.environ.get("API_VERSION", "2023-04-01"),
            # A model version, not an API version; pinned so the model doesn't change under us between runs
            training_config_version=os.environ.get("TRAINING_CONFIG_VERSION", "2023-04-15"),
            staging_deployment=os.environ.get("STAGING_DEPLOYMENT", "staging"),
            # The orchestrator routes to the child's production-v1 deployment, so that is the slot to promote to
            production_deployment=os.environ.get("PRODUCTION_DEPLOYMENT", "production-v1"),
            deploy=os.environ.get("DEPLOY", "false").lower() == "true",
        )


class CluProject:
    def __init__(self, config: Config, project: str):
        self.name = project
        self.config = config
        self.base = f"{config.endpoint}/language/authoring/analyze-conversations/projects/{project}"
        self.headers = {"Ocp-Apim-Subscription-Key": config.key, "Content-Type": "application/json"}

    def request(self, method: str, path: str, body: dict = None) -> requests.Response:
        response = requests.request(method, f"{self.base}{path}", params={"api-version": self.config.api_version},
                                    headers=self.headers, json=body, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(f"[{self.name}] {method} {path} failed ({response.status_code}): {response.text}")
        return response

    def get(self, path: str) -> dict:
        return self.request("GET", path).json()

    def run_job(self, method: str, path: str, body: dict) -> dict:
        print(f"[{self.name}] {method} {path}")
        return poll(self.request(method, path, body).headers["Operation-Location"], self.headers)

    def predict(self, text: str, deployment: str) -> dict:
        response = requests.post(
            f"{self.config.endpoint}/language/:analyze-conversations",
            params={"api-version": self.config.api_version}, headers=self.headers, timeout=30,
            json={
                "kind": "Conversation",
                "analysisInput": {"conversationItem": {"id": "1", "participantId": "smoke-test", "text": text}},
                "parameters": {"projectName": self.name, "deploymentName": deployment},
            },
        )
        response.raise_for_status()
        return response.json()["result"]["prediction"]


def poll(operation_url: str, headers: dict, timeout_s: int = 3600, interval_s: int = 30) -> dict:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        job = requests.get(operation_url, headers=headers, timeout=30).json()
        status = job.get("status")
        print("status:", status)
        if status == "succeeded":
            return job
        if status in ("failed", "cancelled"):
            raise RuntimeError(f"Azure job {status}: {json.dumps(job.get('errors'), indent=2)}")
        time.sleep(interval_s)
    raise TimeoutError("Job did not finish within the timeout")


# ---------------------------------------------------------------------------------------------------
# Training data
# ---------------------------------------------------------------------------------------------------

def _utf16_offset(text: str, index: int) -> int:
    # The dataset declares stringIndexType Utf16CodeUnit; Python indexes code points
    return len(text[:index].encode("utf-16-le")) // 2


def _utf16_slice(text: str, offset: int, length: int) -> str:
    return text.encode("utf-16-le")[offset * 2:(offset + length) * 2].decode("utf-16-le")


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def build_vocabulary(base: dict, synonyms) -> dict[str, set[str]]:
    """Entity terms to label in approved utterances: warehouse synonyms plus every term labelled in the seed set."""
    vocabulary: dict[str, set[str]] = {}
    for category, term in synonyms:
        entity = SYNONYM_ENTITIES.get(category)
        if entity and term and term.strip():
            vocabulary.setdefault(entity, set()).add(term.strip())
    # The seed labels also cover entities with no synonym category, such as AssetType
    for utterance in base["assets"]["utterances"]:
        for label in utterance.get("entities", []):
            if label["category"] not in PATTERN_ENTITIES:
                term = _utf16_slice(utterance["text"], label["offset"], label["length"])
                vocabulary.setdefault(label["category"], set()).add(term)
    return vocabulary


def label_entities(text: str, vocabulary: dict[str, set[str]]) -> list[dict]:
    """Labels known entity terms in an approved utterance.

    CLU treats every unlabelled span of a training utterance as "not an entity", so importing approved
    utterances bare would teach the model to stop extracting the very terms consultants use.
    """
    spans = []
    for entity, terms in vocabulary.items():
        for term in terms:
            for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE):
                spans.append((match.start(), match.end(), entity))
    for entity, pattern in PATTERN_ENTITIES.items():
        for match in pattern.finditer(text):
            spans.append((match.start(1), match.end(1), entity))

    # Where spans overlap, the longest wins ("solid tumors" over "tumors")
    labels, taken = [], []
    for start, end, entity in sorted(spans, key=lambda span: (span[0] - span[1], span[0])):
        if any(start < taken_end and end > taken_start for taken_start, taken_end in taken):
            continue
        taken.append((start, end))
        offset = _utf16_offset(text, start)
        labels.append({"category": entity, "offset": offset, "length": _utf16_offset(text, end) - offset})
    return sorted(labels, key=lambda label: label["offset"])


def build_project_payload(base: dict, approved, synonyms, project: str) -> dict:
    """The import payload: the seed dataset plus approved utterances, with entities labelled."""
    payload = copy.deepcopy(base)
    payload["metadata"]["projectName"] = project
    assets = payload["assets"]
    language = payload["metadata"]["language"]
    known_intents = {intent["category"] for intent in assets["intents"]}
    vocabulary = build_vocabulary(base, synonyms)
    # Seed utterances win over approved duplicates: the seed set is reviewed in pull requests
    seen = {_normalise(utterance["text"]) for utterance in assets["utterances"]}

    added = skipped = 0
    for text, intent in approved:
        text = (text or "").strip()
        key = _normalise(text)
        if intent not in known_intents or not text or len(text) > MAX_AUTHORING_UTTERANCE_CHARS or key in seen:
            skipped += 1
            continue
        seen.add(key)
        assets["utterances"].append(
            {"text": text, "language": language, "intent": intent, "entities": label_entities(text, vocabulary)}
        )
        added += 1

    # Counts only: approved utterances can name confidential targets and CI logs are widely readable
    print(f"Merged {added} approved utterances into {len(base['assets']['utterances'])} seed utterances "
          f"({skipped} skipped: unknown intent, too long, empty or duplicate)")
    return payload


def load_private_key() -> bytes:
    from cryptography.hazmat.primitives import serialization

    pem = os.environ.get("SNOWFLAKE_PRIVATE_KEY") or Path(os.environ["SNOWFLAKE_PRIVATE_KEY_FILE"]).read_text()
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    key = serialization.load_pem_private_key(pem.encode(), password=passphrase.encode() if passphrase else None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def fetch_warehouse_rows():
    """Human-approved training utterances and the synonym vocabulary used to label them."""
    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        private_key=load_private_key(),
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        role=os.environ.get("SNOWFLAKE_ROLE"),
        database="ALIRA_DW",
        schema="ASSISTANT",
        session_parameters={"QUERY_TAG": "AliraCluRetrain"},
    )
    try:
        cs = conn.cursor()
        cs.execute("SELECT raw_utterance, suggested_intent FROM alira_dw.assistant.v_approved_training_data")
        approved = cs.fetchall()
        cs.execute("SELECT category, user_input FROM alira_dw.assistant.vocabulary_synonyms")
        synonyms = cs.fetchall()
    finally:
        conn.close()
    return approved, synonyms


# ---------------------------------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------------------------------

def intent_metrics(evaluation: dict) -> tuple:
    """Intent macro F1 and precision from an evaluation summary.

    The documented Conversation summary has intentsEvaluation at the top level; some payloads nest it under
    customConversationalSummary. Anything else fails closed rather than reading as a score of zero.
    """
    for container in (evaluation, evaluation.get("customConversationalSummary") or {}):
        intents = container.get("intentsEvaluation")
        if intents:
            return intents.get("macroF1"), intents.get("macroPrecision")
    raise SystemExit("Deployment blocked: the evaluation summary has no intentsEvaluation. "
                     "Check the raw payload above and pin the key path in intent_metrics().")


def check_quality_gate(evaluation: dict) -> None:
    f1, precision = intent_metrics(evaluation)
    for metric, value in (("macro F1", f1), ("macro precision", precision)):
        # Catches a schema that reports percentages, which would sail past 0.85 on any model
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise SystemExit(f"Deployment blocked: {metric} is {value!r}, not a score between 0 and 1.")
    print(f"macro F1 {f1:.3f} | macro precision {precision:.3f}")
    if f1 < MIN_MACRO_F1 or precision < MIN_MACRO_PRECISION:
        raise SystemExit(
            f"Deployment blocked: F1 {f1:.3f} / precision {precision:.3f} "
            f"below thresholds {MIN_MACRO_F1} / {MIN_MACRO_PRECISION}"
        )


def regression_failures(project: CluProject, deployment: str, expected_key: str) -> tuple[int, list[str]]:
    """Replays the regression set; expected_key is "intent" for the child model, "practice" for the orchestrator."""
    cases = [case for case in json.loads(REGRESSION_SET.read_text(encoding="utf-8"))["cases"] if expected_key in case]
    failures = []
    for case in cases:
        prediction = project.predict(case["text"], deployment)
        problems = []
        if prediction.get("topIntent") != case[expected_key]:
            problems.append(f"top intent {prediction.get('topIntent')!r}, expected {case[expected_key]!r}")
        if expected_key == "intent":
            extracted = {entity["category"]: entity["text"].lower() for entity in prediction.get("entities", [])}
            for category, expected_text in case.get("entities", {}).items():
                if extracted.get(category) != expected_text.lower():
                    problems.append(f"{category} {extracted.get(category)!r}, expected {expected_text!r}")
        if problems:
            failures.append(f"  {case['text']}: {'; '.join(problems)}")
    return len(cases), failures


def run_smoke_tests(project: CluProject, deployment: str, expected_key: str) -> None:
    total, failures = regression_failures(project, deployment, expected_key)
    print(f"[{project.name}/{deployment}] regression set: {total - len(failures)}/{total} passed")
    if failures:
        raise SystemExit(f"Promotion blocked: {project.name}/{deployment} failed regression cases:\n" + "\n".join(failures))


def prune_trained_models(project: CluProject, keep: int = KEEP_UNDEPLOYED_MODELS) -> None:
    """Deletes the oldest undeployed models so training never hits CLU's cap of 10 models per project."""
    try:
        models = project.get("/models").get("value", [])
        deployments = project.get("/deployments").get("value", [])
    except RuntimeError as exc:
        # A project imported for the first time has neither yet
        print(f"[{project.name}] skipping model pruning: {exc}")
        return
    deployed_ids = {deployment.get("modelId") for deployment in deployments}
    if None in deployed_ids or any(model.get("modelId") is None for model in models):
        print(f"[{project.name}] skipping model pruning: can't tell which models are deployed")
        return
    undeployed = sorted((m for m in models if m["modelId"] not in deployed_ids),
                        key=lambda m: m.get("lastTrainedDateTime", ""), reverse=True)
    for model in undeployed[keep:]:
        print(f"[{project.name}] deleting old model {model['label']}")
        project.request("DELETE", f"/models/{model['label']}")


# ---------------------------------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------------------------------

def retrain_child(config: Config, label: str) -> CluProject:
    child = CluProject(config, config.project)
    prune_trained_models(child)

    base = json.loads(BASE_DATASET.read_text(encoding="utf-8"))
    approved, synonyms = fetch_warehouse_rows()
    child.run_job("POST", "/:import", build_project_payload(base, approved, synonyms, config.project))

    child.run_job("POST", "/:train", {
        "modelLabel": label,
        "trainingMode": "standard",
        "trainingConfigVersion": config.training_config_version,
        "evaluationOptions": {"kind": "percentage", "trainingSplitPercentage": 80, "testingSplitPercentage": 20},
    })

    evaluation = child.get(f"/models/{label}/evaluation/summary-result")
    print("raw evaluation payload:", json.dumps(evaluation))
    check_quality_gate(evaluation)

    child.run_job("PUT", f"/deployments/{config.staging_deployment}", {"trainedModelLabel": label})
    run_smoke_tests(child, config.staging_deployment, "intent")
    return child


def retrain_orchestrator(config: Config, label: str) -> None:
    """Connected intents copy the child's utterances when the orchestrator trains, so routing only learns
    from approved feedback once the orchestrator is retrained against the newly promoted child."""
    orchestrator = CluProject(config, ORCHESTRATOR_PROJECT)
    prune_trained_models(orchestrator)
    # Same split as deploy_clu.py: the orchestrator's own utterances are too few to hold out a test set
    orchestrator.run_job("POST", "/:train", {
        "modelLabel": label, "trainingMode": "standard", "evaluationOptions": {"kind": "manual"},
    })
    orchestrator.run_job("PUT", f"/deployments/{config.staging_deployment}", {"trainedModelLabel": label})
    run_smoke_tests(orchestrator, config.staging_deployment, "practice")
    orchestrator.run_job("PUT", f"/deployments/{config.production_deployment}", {"trainedModelLabel": label})


def main() -> None:
    config = Config.from_env()
    label = f"v{int(time.time())}"

    child = retrain_child(config, label)
    if not config.deploy:
        print(f"Gate-only run (DEPLOY is not true): {label} passed and stays in {config.staging_deployment}")
        return

    child.run_job("PUT", f"/deployments/{config.production_deployment}", {"trainedModelLabel": label})
    retrain_orchestrator(config, label)
    print(f"Deployed {label} to production")


if __name__ == "__main__":
    main()
