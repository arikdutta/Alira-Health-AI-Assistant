# DPIA — Alira assistant: telemetry and feedback loop

**Status:** DRAFT. Must be signed off before pilot launch.
**Scope:** Phase 5 observability, the NLU feedback loop and CLU retraining.
**Prepared from:** `telemetry.py`, `snowflake_engine.py`, `main.py`, `sql/phase5_observability.sql`, `scripts/retrain_clu.py`, `monitoring/observability.bicep`.

Sections 1–4 describe what the system does, as implemented. Sections 5 and 6 need decisions from the controller and the DPO.

---

## 1. Why this processing exists

The assistant turns a consultant's question, such as *"Show me oncology targets in Germany with revenue over 20M"*, into a warehouse query. To keep it accurate, the system:

- measures routing confidence, zero-row answers and failures (Application Insights), and
- captures questions it handled badly so a data steward can turn them into synonyms and training examples (Snowflake `NLU_FEEDBACK`).

**The main risk:** a consultant's question can name a live, confidential M&A target. If it is stored next to the consultant's identity, the monitoring stack itself discloses who is looking at which deal.

## 2. Data inventory

| Store | What is stored | Identifies a person? | Retention | Who can read it |
|---|---|---|---|---|
| App Insights `AppEvents` (events `QueryRouted`, `LowConfidenceFallback`, `UnmappedTerm`, `QueryFailed`) | `trace_id`, `practice`, `intent`, `confidence`, `latency_ms`, `row_count`, `outcome`, `user_hash`, `redacted_utterance`, redacted `detected_term`, `detected_category`, `error_type` | Pseudonymous (`user_hash`) | **30 days**, no archive (`observability.bicep`) | Readers of the Log Analytics workspace |
| App Insights `AppTraces`, `AppExceptions` | Operational log lines and stack traces. No utterances by design. | No | **30 days** (backstop) | As above |
| App Insights `AppRequests`, `AppDependencies` | Endpoint paths, status codes, durations. Client IP is masked by App Insights' default setting. | No | Workspace default — **decide (§6)** | As above |
| Snowflake `ASSISTANT.NLU_FEEDBACK` | **Full** utterance, `trace_id`, failure type, detected term, the reviewer's Snowflake username | The utterance may contain confidential business data. The reviewer is identified. | **Undefined — decide (§6)** | `ALIRA_FEEDBACK_APP`, via the review app, for `ALIRA_DATA_STEWARD` only |
| Snowflake `TELEMETRY_IDENTITY.USER_HASH_MAP` | `user_hash` → Entra object id | **Yes** (re-identification key) | **Undefined — decide (§6)** | Insert-only for the API. Read access: `ALIRA_PRIVACY_OFFICER` only. |
| Azure CLU project `alira-transaction-advisory` (and `-ci`) | Approved utterances, in full, as training data | Confidential business data | Until the next weekly import replaces them | Language resource owners and contributors |
| GitHub Actions logs | Counts of merged utterances, evaluation metrics, regression-set results (synthetic text) | No | GitHub default (90 days) | Repository members |
| Snowflake query history (existing, pre-Phase 5) | `QUERY_TAG = AliraBot-<user id>` on every assistant query | **Yes** | Snowflake account default | Account admins and roles with `MONITOR` |

## 3. Data flow

1. A consultant asks a question in Teams or the web portal.
2. `snowflake_engine.query_alira_assistant` sends it to Azure CLU and, if it is routed, queries Snowflake.
3. **Telemetry path** (`telemetry.emit_event`): the user becomes `user_hash`. The utterance passes through `UtteranceRedactor` and the result is sent to Application Insights.
4. **Feedback path** (`record_feedback`): on low confidence, an unmapped term, a zero-row answer or a consultant's "Report wrong result", the **full** utterance is written to `NLU_FEEDBACK` in the background.
5. The steward approves or rejects rows in the Streamlit-in-Snowflake app. Approving an unmapped term also adds a row to `VOCABULARY_SYNONYMS`.
6. Every Monday, `retrain_clu.py` reads **approved rows only** (`V_APPROVED_TRAINING_DATA`), imports them into CLU and trains. It deploys only if the quality gate and regression set pass.

## 4. Controls in place

| Rule (Phase 5.2) | Implementation |
|---|---|
| Pseudonymise the user | `user_hash = HMAC-SHA256(TELEMETRY_HMAC_SECRET, oid)`. The API refuses to start with telemetry enabled if the secret is missing (`main.py`). `emit_event` rejects dimensions named `UserEmail`, `RawUtterance`, `email`, `oid` and similar. |
| Separate, access-controlled reverse mapping | `TELEMETRY_IDENTITY` is a managed-access schema. The API role can insert but not select. Only `ALIRA_PRIVACY_OFFICER` can read, and that role is deliberately not granted to `SYSADMIN`. |
| Redact utterances before logging | Company names from `V_COMPANY_NAME_MASTER` (targets table plus `REDACTION_WATCHLIST`) become `[COMPANY]`. All numeric figures become `[NUM]`. **Fails closed:** until the list has loaded, utterances are replaced with `[WITHHELD]`. The list refreshes hourly. |
| No incidental leakage | `QueryFailed` records the exception *type* only, never the message. The retraining job prints counts, not utterances. |
| Full utterance only under DW governance | Stored only in `NLU_FEEDBACK` and, once approved, in the CLU project. |
| Explicit retention | 30 days on `AppEvents`, `AppTraces` and `AppExceptions`, with total retention equal to interactive retention, so there is no archive tier. |
| Human in the loop | Retraining reads only rows a named steward approved. `reviewed_by` records the viewer's identity (`st.user`), not the app owner's. |

## 5. Residual risks

| # | Risk | Likelihood / impact | Mitigation now | Further option |
|---|---|---|---|---|
| R1 | **Redaction only catches exact names.** Nicknames, abbreviations ("Kura" for "Kura Therapeutics DE"), misspellings and codenames not on the watchlist reach telemetry. | Medium / High | Deal teams add aliases and codenames to `REDACTION_WATCHLIST`. 30-day retention. Workspace access restricted. | Drop `redacted_utterance` from events entirely; diagnostics then rely on `NLU_FEEDBACK` only. |
| R2 | **Personal data inside utterances** (for example a named executive) is not redacted. | Low / Medium | Retention and access as above. | Add a person-name detector (Azure AI Language PII) before logging. |
| R3 | **Re-identification by brute force.** Anyone holding `TELEMETRY_HMAC_SECRET` and a list of staff object ids can recompute every hash. | Low / High | Store the secret in Key Vault and grant it to the API identity only. | Define secret rotation. Note that rotating breaks linkage with older hashes. |
| R4 | **The steward sees full utterances**, including confidential targets. | Certain / Medium | The steward is a senior consultant already cleared for deal data. The app shows a do-not-copy notice. | Row access policy by practice area. |
| R5 | **`/query` and `/feedback` have no authentication** in the current sandbox build. The web portal uses a mock user, and anyone who can reach the API can add rows to `NLU_FEEDBACK`. | High / Medium | None yet. | **Blocking for pilot:** put both endpoints behind Entra ID (the `Data.Query` scope from the blueprint) and pass the real `oid`. |
| R6 | **Snowflake `QUERY_TAG` carries the user id** next to every warehouse query. This predates Phase 5. | Certain / Medium | None. | Tag queries with `user_hash` instead. |
| R7 | **Unbounded retention** of `NLU_FEEDBACK` and `USER_HASH_MAP`. | Certain / Medium | None. | Scheduled delete task once retention is decided (§6). |
| R8 | **Branch and PR retraining runs** import approved utterances into the `-ci` CLU project. | Certain / Low | Same Language resource, same access. | Delete the `-ci` project after each run. |

## 6. Decisions needed before sign-off

- [ ] Controller, processor roles and lawful basis (legitimate interest assessment for consultant monitoring).
- [ ] Retention for `NLU_FEEDBACK`: rejected rows, approved rows, and approved rows once they are part of a trained model.
- [ ] Retention for `USER_HASH_MAP`. Recommendation: no longer than the 30-day telemetry it re-identifies.
- [ ] Retention for `AppRequests` and `AppDependencies`.
- [ ] Azure and Snowflake regions, and whether any data leaves the EU.
- [ ] Works council or employee-representative consultation, given this monitors consultants' use of a tool.
- [ ] Who holds `ALIRA_PRIVACY_OFFICER`, and the procedure and audit trail for a re-identification request.
- [ ] Accept, mitigate or block on each of R1–R8. R5 is recommended as blocking.
- [ ] Consultant-facing privacy notice text.

| Role | Name | Date | Decision |
|---|---|---|---|
| DPO | | | |
| Engineering lead | | | |
| Data steward | | | |
