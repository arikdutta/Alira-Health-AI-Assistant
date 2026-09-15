"""Imports, trains and deploys the CLU child model and the orchestrator that routes to it.

Usage: python deploy_clu.py [dataset.json ...]

With no arguments both datasets are deployed. To publish orchestrator-only changes, such as a new practice
intent, pass just clu_orchestrator_dataset.json: re-importing the child from its seed file would drop the
approved utterances scripts/retrain_clu.py merged into it.
"""
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_VERSION = "2023-04-01"
DEPLOYMENT_NAME = "production-v1"
MODEL_LABEL = "v1"
PROJECTS_URL = os.environ["AZURE_LANGUAGE_ENDPOINT"].rstrip("/") + "/language/authoring/analyze-conversations/projects"
HEADERS = {"Ocp-Apim-Subscription-Key": os.environ["AZURE_LANGUAGE_KEY"]}

# Child projects must be deployed before the orchestrator, which trains against their deployments
DATASETS = ["clu_base_dataset.json", "clu_orchestrator_dataset.json"]


def start_job(method, url, body):
    response = requests.request(method, url, params={"api-version": API_VERSION}, headers=HEADERS, json=body)
    if response.status_code >= 400:
        sys.exit(f"{method} {url} failed ({response.status_code}): {response.text}")
    return response.headers["operation-location"]


def wait_for_job(job_url, step):
    while True:
        job = requests.get(job_url, headers=HEADERS).json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(5)
    if job["status"] != "succeeded":
        sys.exit(f"{step} {job['status']}: {json.dumps(job.get('errors'), indent=2)}")
    print(f"{step}: succeeded")


def deploy_project(dataset_path):
    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)
    project_name = dataset["metadata"]["projectName"]
    project_url = f"{PROJECTS_URL}/{project_name}"

    # Manual split with no utterances tagged "Test" trains on the whole (small) seed set
    train_body = {"modelLabel": MODEL_LABEL, "trainingMode": "standard", "evaluationOptions": {"kind": "manual"}}

    wait_for_job(start_job("POST", f"{project_url}/:import", dataset), f"[{project_name}] import")
    wait_for_job(start_job("POST", f"{project_url}/:train", train_body), f"[{project_name}] train")
    wait_for_job(
        start_job("PUT", f"{project_url}/deployments/{DEPLOYMENT_NAME}", {"trainedModelLabel": MODEL_LABEL}),
        f"[{project_name}] deploy",
    )


if __name__ == "__main__":
    for dataset_path in sys.argv[1:] or DATASETS:
        deploy_project(dataset_path)
