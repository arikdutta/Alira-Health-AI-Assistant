# Alira Health AI Assistant — Architecture Deep-Dive

> **Alira Health AI Assistant** — An NLU-routed, agent-assisted consulting assistant that answers questions from a governed Snowflake warehouse, with privacy-safe telemetry and human-approved, metric-gated retraining.

## Documentation index

| Doc | When to read it |
|-----|-----------------|
| [`README.md`](README.md) | How to run the whole system: env vars, API, tests, known gaps |
| This file | How the pieces fit together, why each one exists, one-time cloud setup |
| [`docs/DPIA_assistant_telemetry.md`](docs/DPIA_assistant_telemetry.md) | What is stored where and for how long, privacy controls, residual risks |
| [`sql/phase5_observability.sql`](sql/phase5_observability.sql) | Who can do what: the enforced Snowflake role split |
| [`agent_config.yaml`](agent_config.yaml) | The Foundry agent's instructions and MCP tool allowlist |
| [`market_access_model.yaml`](market_access_model.yaml) | The semantic model Cortex Analyst writes market access SQL from |
| [`clu_regression_set.json`](clu_regression_set.json) | Cases every retrained model must pass before promotion |
| [`monitoring/kql/`](monitoring/kql/) | Dashboards: unmapped terms, routing confidence drift, zero-row queries |

## What Are We Actually Building?

We're building an **internal AI assistant for healthcare consultants** at Alira Health. Consultants work across practices (M&A transaction advisory, market access, real-world evidence), and the answers they need sit in a Snowflake data warehouse. Instead of writing SQL or waiting for an analyst, they ask in plain English, in a web portal or Microsoft Teams, and get warehouse rows back. Behind one API, the system has **two answer engines**:

**Engine 1 — CLU + bound SQL (the "screener"):**
1. A consultant asks "Show me oncology targets in Germany with revenue over 20M"
2. An Azure CLU **orchestrator** routes the question to the M&A practice, and a **child CLU model** extracts the intent (`ScreenTargets`) and entities (*oncology*, *Germany*, *20M*)
3. A **synonym table** turns each term into the code the data uses (*oncology* → `Oncology`, *Germany* → `DE`)
4. The codes are **bound as SQL parameters**, never concatenated, and matching targets come back as a table
5. It is deterministic: the same question always runs the same query

**Engine 2 — Foundry agent + Snowflake MCP (the "analyst"):**
1. A consultant asks "What is the reimbursement status of Keytruda in France?" or "How many NSCLC patients with EGFR mutations are in registries?"
2. The orchestrator routes it to `Alira-MarketAccess` or `Alira-RWE`, practices with too many question shapes for fixed SQL
3. An **Azure AI Foundry prompt agent** picks tools from a Snowflake-managed **MCP server**: Cortex Analyst writes the SQL, `run_sql` runs it, Cortex Search finds registry cohorts
4. The agent answers in prose above the rows its tools returned. It is instructed never to estimate figures or write SQL itself

**The loop around both engines:**
1. Every miss (low confidence, an unmapped term, zero rows, a "Report wrong result" click) lands in `NLU_FEEDBACK`
2. A **data steward** approves or rejects each row in a Streamlit-in-Snowflake app. Mapping an unmapped term fixes the query for everyone immediately
3. Every Monday, GitHub Actions **retrains CLU on approved rows only** and promotes the model only if it clears an F1 gate and a regression set
4. Telemetry reaches Application Insights with **pseudonymised users and redacted questions**, so the monitoring stack can't reveal who is looking at which deal

**Why is this pattern worth learning?** Any company that puts natural language in front of its warehouse hits the same problems: questions that must return exactly the right rows, a long tail no fixed query covers, figures a model must never invent, vocabulary that drifts, and logs that must not leak confidential names. This project answers each one with a deliberate component. Deterministic routing handles the questions where precision matters, a tool-restricted agent handles the long tail, and a human-approved loop improves both over time. That makes it a reusable template.

---

## The Full System — One Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│        PRESENTATION TIER (consultant's browser · Microsoft Teams)         │
│  React 19 portal (Vite 8)                          Microsoft Teams bot    │
│  prompt box · results table                        @mention or 1:1 chat   │
│  "Report wrong result" link                        Adaptive Card table    │
└─────────────┬────────────────────────────────────────────────┬────────────┘
              │ POST /query, /feedback      POST /api/messages │
┌─────────────▼────────────────────────────────────────────────▼────────────┐
│                 APPLICATION TIER (FastAPI · Python 3.12)                  │
│  ┌─────────────────────┐ ┌───────────────────────┐ ┌───────────────────┐  │
│  │ GATEWAY  main.py    │ │ PRIVACY  telemetry.py │ │ TEAMS ADAPTER     │  │
│  │ Pydantic · CORS     │ │ HMAC user_hash        │ │ teams_bot.py      │  │
│  │ X-Trace-Id header   │ │ fail-closed redaction │ │ Bot Framework JWT │  │
│  └──────────┬──────────┘ └───────────────────────┘ └─────────┬─────────┘  │
│             │                                                │            │
│  ┌──────────▼────────────────────────────────────────────────▼─────────┐  │
│  │             BUSINESS LOGIC LAYER  (snowflake_engine.py)             │  │
│  │ · route to a practice; fall back below the confidence threshold     │  │
│  │ · map terms via VOCABULARY_SYNONYMS, bind them as SQL parameters    │  │
│  │ · one response shape {type, message, data} for every engine         │  │
│  │ · queue misses into NLU_FEEDBACK on a background thread             │  │
│  └───────┬─────────────────┬─────────────────┬─────────────────┬───────┘  │
│          │                 │                 │                 │          │
│  ┌───────▼───────┐ ┌───────▼───────┐ ┌───────▼───────┐ ┌───────▼───────┐  │
│  │ CLU ROUTING   │ │ WAREHOUSE I/O │ │ AGENT BRIDGE  │ │ TELEMETRY     │  │
│  │ practice →    │ │ bound SQL +   │ │ foundry_      │ │ 4 events ·    │  │
│  │ child intent  │ │ feedback rows │ │ orchestrator  │ │ 7 dimensions  │  │
│  └───────┬───────┘ └───────┬───────┘ └───────┬───────┘ └───────┬───────┘  │
└──────────┼─────────────────┼─────────────────┼─────────────────┼──────────┘
           │                 │                 │                 │
   ┌───────▼───────┐         │         ┌───────▼───────┐ ┌───────▼───────┐
   │ AZURE AI      │         │         │ AZURE AI      │ │ AZURE MONITOR │
   │ LANGUAGE      │         │         │ FOUNDRY       │ │ App Insights  │
   │ orchestrator  │         │         │ prompt agent  │ │ 30-day logs   │
   │ + child model │         │         │ MCP allowlist │ │ KQL workbook  │
   └───────────────┘         │         └───────┬───────┘ └───────────────┘
                             │ bound SQL       │ MCP tools
┌────────────────────────────▼─────────────────▼────────────────────────────┐
│         DATA TIER: SNOWFLAKE  ALIRA_DW  (warehouse ALIRA_BOT_WH)          │
│  ASSISTANT schema                          MCP server tools (Cortex)      │
│  M_AND_A_TARGETS · VOCABULARY_SYNONYMS     market_access_analyst          │
│  NLU_FEEDBACK · REDACTION_WATCHLIST        run_sql                        │
│  V_APPROVED_TRAINING_DATA                  rwe_registry_search            │
│  V_COMPANY_NAME_MASTER                                                    │
│  NLU_FEEDBACK_REVIEW (Streamlit app)                                      │
│  TELEMETRY_IDENTITY.USER_HASH_MAP (managed access)                        │
└───────────────────┬───────────────────────────────────────────────────────┘
                    │ approved rows only · every Monday 02:00 UTC
        ┌───────────▼───────────────────────────────┐
        │ GITHUB ACTIONS  retrain_clu.yml           │
        │ merge seed + approved → import → train    │
        │ gate: macro F1 ≥ 0.85 · precision ≥ 0.80  │──► Azure AI Language
        │ staging → regression set → production-v1  │
        └───────────────────────────────────────────┘
```

**Three tiers:**
- **Presentation** — React portal and Teams bot. They display answers and hold no business logic. Both call the same engine, and the portal renders every answer from one `{type, message, data}` payload.
- **Application** — FastAPI. Routing decisions, validation, SQL construction, feedback capture, pseudonymisation and redaction all live here. Azure AI Language and Azure AI Foundry are managed services this tier calls. The Foundry agent then reaches Snowflake on its own, through the MCP server, not through FastAPI.
- **Data** — Snowflake is the system of record: targets, synonyms, feedback, the redaction watchlist and the identity map, plus the Cortex services and MCP server the agent uses. Application Insights holds redacted telemetry for 30 days.

---

## Phase 0: Why Each Component Exists

### 1. Azure AI Language (CLU) — The Router

**What:** Two Conversational Language Understanding projects. `Alira-Master-Orchestrator` is an *orchestration* project that decides which practice a question belongs to. `alira-transaction-advisory` is a *conversation* project (the "child") that extracts the intent and entities for M&A questions.

**Why not send every question to an LLM?** Because screening M&A targets needs the same query for the same question, every time. CLU gives you:
- **Confidence scores you can threshold:** the orchestrator falls back to `None` below 0.5 and the child below 0.7, and the assistant says it didn't understand instead of guessing
- **Training data you can review:** datasets are JSON files in the repo, changed through pull requests
- **Metrics you can gate on:** every training run reports macro F1 and precision

**How it works:**
1. The orchestrator scores four intents: `Alira-MA-DueDiligence`, `Alira-MarketAccess`, `Alira-RWE` and `None`
2. `Alira-MA-DueDiligence` is a **connected intent**: the orchestrator calls the child's `production-v1` deployment and nests the child's prediction inside its own response
3. The child predicts `ScreenTargets`, `GetDealComparables` or `None`, and extracts `TherapeuticArea`, `Geography`, `AssetType`, `RevenueFloorUsd` and `YearFrom`
4. The other two practices have no child model. They go to the Foundry agent

**Datasets:** [`clu_orchestrator_dataset.json`](clu_orchestrator_dataset.json) (routing) and [`clu_base_dataset.json`](clu_base_dataset.json) (child seed set). [`deploy_clu.py`](deploy_clu.py) deploys both the first time, child first, because the orchestrator trains against the child's deployment.

**What you'll learn:** Orchestration vs. conversation projects, connected intents, confidence thresholds and fallbacks, entity labelling.

---

### 2. Snowflake — The Data Tier

**What:** The warehouse where the consulting data already lives, in database `ALIRA_DW`. The assistant reads and writes the `ASSISTANT` schema. A separate managed-access schema holds the identity map.

**Why query the warehouse directly instead of copying data out?** The data, its governance and its roles are already there. Querying in place means there is no second copy to secure, and Snowflake roles give every component least privilege. Snowflake also hosts the Cortex services, the MCP server and the steward's review app.

**Objects:**
| Object | Purpose |
|--------|---------|
| `M_AND_A_TARGETS` | Company, therapeutic area, ISO country, annual revenue, pipeline stage, asset type (mock data) |
| `VOCABULARY_SYNONYMS` | Consultant term → master code, by category (`THERAPEUTIC_AREA`, `GEOGRAPHY`) |
| `NLU_FEEDBACK` | Questions the assistant handled badly, with failure type and review status |
| `V_APPROVED_TRAINING_DATA` | Approved feedback only: the one source retraining may read |
| `REDACTION_WATCHLIST` | Confidential target names and codenames added by deal teams |
| `V_COMPANY_NAME_MASTER` | Targets plus watchlist: the names telemetry redaction strips |
| `TELEMETRY_IDENTITY.USER_HASH_MAP` | `user_hash` → Entra object id. The API can insert but not read |
| `NLU_FEEDBACK_REVIEW` | Streamlit-in-Snowflake app for the data steward |
| `ALIRA_BOT_WH` | SMALL warehouse, 1–5 clusters, economy scaling, suspends after 60 s idle |

**Who can do what** (full grants in [`sql/phase5_observability.sql`](sql/phase5_observability.sql)):
| Role | Can | Cannot |
|------|-----|--------|
| `ALIRA_ASSISTANT_API` | Read targets, synonyms and the company master list; insert feedback; insert into the identity map | Read the identity map, update feedback |
| `ALIRA_FEEDBACK_APP` | Read and update feedback; read and insert synonyms. The review app runs as this role | Touch the identity map |
| `ALIRA_DATA_STEWARD` | Open the review app | Query the tables directly |
| `ALIRA_CLU_RETRAIN` | Read approved training rows and synonyms | Read pending or rejected feedback |
| `ALIRA_PRIVACY_OFFICER` | Read the identity map, i.e. re-identify a `user_hash` | Be inherited by `SYSADMIN` (deliberately not granted) |

The agent's `run_sql` tool runs as the role behind the Snowflake PAT, so that role's grants are the ceiling on what the agent can read.

**What you'll learn:** Least-privilege role design, managed-access schemas, key-pair authentication, query tags, multi-cluster warehouses.

---

### 3. The Synonym Matrix — The Safety Layer

**What:** `VOCABULARY_SYNONYMS`, a lookup from what consultants type to the codes the data stores.

**Why not put the extracted entity straight into the SQL?** Two reasons:
- **Vocabulary:** consultants say *cancer*, *solid tumors* or *oncology*, but the table stores `Oncology`. They say *Germany* or *de*, but the table stores `DE`. Without a mapping, correct questions return zero rows.
- **Injection:** entity text is user input. The engine looks the term up with a bound parameter, then binds the resulting master code into the target query. User text never becomes SQL.

**How it works:**
1. For `TherapeuticArea` and `Geography`, `lookup_master_code` runs `WHERE category=%s AND LOWER(user_input)=LOWER(%s)`
2. **A term with no master code stops the query.** A filter nothing can match would return zero rows that look like "no targets". Instead the engine emits `UnmappedTerm`, queues an `UNMAPPED_TERM` feedback row and returns an empty result
3. `RevenueFloorUsd` is parsed as a number ("20M" → 20,000,000) and bound as `annual_revenue >= %s`
4. Only `ScreenTargets` has a query today. `GetDealComparables` is trained but answers that no SQL mapping exists

**What you'll learn:** Entity normalisation, parameter binding, failing safely on unknown input.

---

### 4. Azure AI Foundry Agent + Snowflake MCP — The Analyst

**What:** A Foundry **prompt agent**, `alira-master-orchestrator-agent`, defined in [`agent_config.yaml`](agent_config.yaml) and published as a new version with `python foundry_orchestrator.py publish`. Its only tool is a Snowflake-managed **MCP (Model Context Protocol) server**.

**Why an agent for these practices, and CLU for M&A?** Market access and RWE questions come in too many shapes for fixed queries: any drug, any payer, any biomarker, any registry. An agent can choose the right tool and fill in the specifics. M&A screening has a small, stable shape, so it stays on the deterministic path.

**Why MCP instead of letting the model write SQL?** The MCP server exposes a fixed set of warehouse tools, and the agent may call only the ones on its allowlist:

| Tool | Backed by | Returns |
|------|-----------|---------|
| `market_access_analyst` | Cortex Analyst over [`market_access_model.yaml`](market_access_model.yaml) | A SQL statement, not results |
| `run_sql` | Snowflake SQL execution | Rows, limited to what the PAT's role can read |
| `rwe_registry_search` | Cortex Search over registry cohorts | Hits with `@scores` (cosine similarity, reranker score) |

**The agent's instructions** tell it to delegate every figure to a tool, run Analyst's SQL exactly as generated, and request only the four real registry columns (`cohort_name`, `biomarker_status`, `country`, `patient_count`).

**Guardrails in the config loader** (`load_agent_config`):
- `Allowed_Tools` must list at least one tool, because an empty list would expose every tool on the server
- `Auth_Token` is rejected. The Snowflake PAT lives in a **Foundry project connection**, because anything in the agent definition is readable by anyone who can read the agent
- The MCP URL must match Snowflake's format, with hyphens rather than underscores in the hostname, which Snowflake's MCP clients require
- Every `${VAR}` reference must resolve from the environment, or loading fails
- `Approval_Mode: always` makes Foundry pause for a human to approve each tool call. This service has no approval step, so a paused response raises an error instead of returning an empty answer

**Reading the answer:** the bridge collects the `mcp_call` items from the response. Tool names become the telemetry `intent`. Tool outputs become flat rows: SQL result sets are unpacked, Cortex Search `@scores` become `scores_*` columns, and Analyst's SQL text yields no rows.

**What you'll learn:** Prompt agents and versioning, MCP tool allowlists, keeping secrets out of agent definitions, `DefaultAzureCredential`.

---

### 5. FastAPI — The Gateway

**What:** One Python service, [`main.py`](main.py), serving both the web portal and the Teams bot.

**Why FastAPI?**
- Pydantic validates request bodies (`/feedback` limits `trace_id` to 64 characters and `prompt` to 1,000)
- One app hosts `/query`, `/feedback` and the Teams endpoint, so both front ends share one engine
- The Azure Monitor OpenTelemetry distro instruments it automatically

**Details that matter:**
- `configure_azure_monitor` runs **before** FastAPI is imported, because instrumentation swaps in a traced `FastAPI` class
- If telemetry export is on and `TELEMETRY_HMAC_SECRET` is missing, the API **refuses to start**
- On startup, the lifespan hook begins loading the company-name list for redaction and registers the Teams route
- `/query` returns the OpenTelemetry trace id in an `X-Trace-Id` header (exposed through CORS), so a "Report wrong result" click links back to the exact request
- `portal_payload` converts whatever the engine returned into `{type, message, data}`, where `type` is `MA_TARGETS`, `MARKET_ACCESS`, `RWE_SEARCH` or `GENERIC_MESSAGE`

**What you'll learn:** Lifespan hooks, import-order-sensitive instrumentation, one response contract for several engines.

---

### 6. React Portal + Teams Bot — The Front Doors

**React portal** ([`frontend/src/AliraDashboard.jsx`](frontend/src/AliraDashboard.jsx)): React 19, Vite 8, inline styles, no UI libraries.
- `MA_TARGETS` rows render in a fixed four-column table. Agent rows render in a table whose columns come from the rows, because each tool returns different fields
- Agent prose appears above its rows
- `GENERIC_MESSAGE` appears as a notice rather than an empty table
- **Report wrong result** posts the prompt and trace id to `/feedback`

**Teams bot** ([`teams_bot.py`](teams_bot.py)): the `microsoft-teams-apps` SDK, mounted on the same FastAPI app.
- Strips only the bot's own @mention, so questions work in channels, group chats and 1:1 chats
- Sends a typing indicator, then runs the blocking engine call in a worker thread (`asyncio.to_thread`) so the event loop stays free
- M&A rows come back as an **Adaptive Card table** capped at 5 rows, since tables get cramped on mobile. Agent answers come back as text
- Bot Framework JWT validation turns on when `CLIENT_ID` is set

**What you'll learn:** Sharing one backend contract across UIs, Adaptive Cards, keeping blocking I/O off an async event loop.

---

### 7. OpenTelemetry + Application Insights — The Observability Layer

**What:** Structured events exported to the Application Insights `customEvents` table, alongside automatic request and dependency traces.

**Why is this critical?** When a consultant says "the assistant got it wrong", you need to see which practice the question was routed to, how confident the model was, what the outcome was, how many rows came back and how long it took. You also need trends: is routing confidence drifting, and which terms keep failing?

**Four events, seven shared dimensions:**
| Event | Emitted when |
|-------|-------------|
| `QueryRouted` | A question was answered. `outcome` is `ROWS`, `NO_ROWS`, `UNMAPPED_TERM`, `NO_SQL_MAPPING`, `AGENT_ROWS` or `AGENT_TEXT` |
| `LowConfidenceFallback` | The orchestrator or the child fell back to `None` |
| `UnmappedTerm` | A filter term had no master code |
| `QueryFailed` | An exception escaped. Only the exception **type** is recorded, because messages from Azure or Snowflake can echo the question |

Every event carries `trace_id`, `practice`, `intent`, `confidence`, `latency_ms`, `row_count` and `user_hash`, so any dashboard can slice by any of them. For agent practices, `intent` is the names of the tools the agent called, joined with `+`.

**Dashboards** in [`monitoring/kql/`](monitoring/kql/), deployed as a workbook by [`monitoring/observability.bicep`](monitoring/observability.bicep):
- `01_unmapped_terms`: the weekly synonym backlog
- `02_routing_confidence_by_practice`: daily P50 and P10 confidence per practice. A falling P10 means consultants are phrasing things the model hasn't seen, and fallbacks will rise next
- `03_zero_row_queries`: the silent failure mode, a success to the system and a broken tool to the consultant

The same Bicep template sets **30-day retention with no archive tier** on `AppEvents`, `AppTraces` and `AppExceptions`.

The agent bridge also writes a local trace log, `logs/orchestration_traces.txt`, with tool names, failures, latency and the redacted prompt. It never records tool arguments or outputs, which carry the question and warehouse data.

**What you'll learn:** Custom events vs. traces, dimension design, KQL, drift monitoring, retention as code.

---

### 8. The Privacy Layer — Pseudonymisation + Redaction

**What:** [`telemetry.py`](telemetry.py), the only path into telemetry.

**Why is this critical?** A consultant's question can name a live, confidential M&A target. If that question sits next to the consultant's identity in a log, the monitoring stack itself discloses who is looking at which deal. That is the main risk in the [DPIA](docs/DPIA_assistant_telemetry.md).

**Controls:**
- **Pseudonymised users:** `user_hash = HMAC-SHA256(TELEMETRY_HMAC_SECRET, oid)`. The reverse mapping is written in the background to `USER_HASH_MAP`, which only `ALIRA_PRIVACY_OFFICER` can read
- **Redacted questions:** company names from `V_COMPANY_NAME_MASTER` become `[COMPANY]`, longest names first so "Kura Therapeutics DE" is replaced whole. Every figure becomes `[NUM]`
- **Fail closed:** until the company list has loaded, questions are replaced with `[WITHHELD]`. The list refreshes hourly, so a name a deal team adds is redacted without a redeploy
- **Tripwire:** `emit_event` raises if a dimension is named `email`, `oid`, `raw_utterance` or similar
- **Full questions stay under warehouse governance:** they are stored only in `NLU_FEEDBACK` and, once approved, in the CLU project

Without a configured secret (local runs, tests), a random per-process key keeps hashes unlinkable but unstable, and those hashes are not written to the identity map.

**What you'll learn:** Keyed pseudonymisation, fail-closed design, separating a re-identification key from analytics, writing a DPIA from the implementation.

---

### 9. Feedback Loop + Review App — The Human in the Loop

**What:** The `NLU_FEEDBACK` table plus [`feedback_review_app/nlu_feedback_review.py`](feedback_review_app/nlu_feedback_review.py), a Streamlit-in-Snowflake app for a named data steward.

**Why?** A model only improves from examples of what it gets wrong, and only someone who knows the business can say what a consultant meant. Automatic capture finds the misses. The steward decides what they mean.

**Failure types:**
| Type | Captured when | Steward action |
|------|--------------|----------------|
| `LOW_CONFIDENCE` | The orchestrator or the child fell back to `None` | Approve with the intent the consultant meant |
| `UNMAPPED_TERM` | A filter term had no master code | Map it to a master code. **This fixes the query for everyone at once**, with no retrain |
| `NO_ROWS` | The SQL ran and matched nothing | Reject if the data simply doesn't exist |
| `WRONG_RESULT` | A consultant clicked "Report wrong result" | Approve with the intent the consultant meant |

**Details that matter:**
- Feedback writes run on a background thread pool, so a failed write never fails a query
- The app runs with its owner role's rights, so it takes the reviewer's name from `st.user`. `CURRENT_USER()` would return the app owner
- Approve and reject only update rows still `PENDING`, so two reviewers can't overwrite each other
- Agent answers don't create feedback rows automatically, because approvals train the child CLU model, which has no intents for those practices. A consultant's report still does

**What you'll learn:** Active learning, human-approved training data, owner's-rights apps, status guards against concurrent edits.

---

### 10. GitHub Actions Retraining — The Quality Gate

**What:** [`.github/workflows/retrain_clu.yml`](.github/workflows/retrain_clu.yml) runs [`scripts/retrain_clu.py`](scripts/retrain_clu.py) every Monday at 02:00 UTC, on demand, and on pull requests that touch the child dataset, the regression set or the script.

**Why weekly, not daily?** Daily model churn can't be reviewed. A week gives the steward time to approve a meaningful batch and gives people time to notice a regression.

**The pipeline:**
```
Prune old undeployed models (keep 5; CLU allows 10 per project)
    ↓
Read V_APPROVED_TRAINING_DATA + VOCABULARY_SYNONYMS as ALIRA_CLU_RETRAIN
    ↓
Merge into the seed set: skip unknown intents, duplicates, empty or >500-char utterances;
label known entity terms (longest span wins, UTF-16 offsets)
    ↓
Import → train (80/20 split, pinned training config 2023-04-15)
    ↓
Macro F1 ≥ 0.85 and macro precision ≥ 0.80? ──No──→ Deployment blocked
    ↓ Yes
Deploy to staging → replay regression set (intent + entities) ──Any case fails──→ Promotion blocked
    ↓ All pass
DEPLOY=true? ──No──→ Stop; the model stays in staging
    ↓ Yes
Promote child to production-v1
    ↓
Retrain orchestrator → staging → replay routing cases ──Any case fails──→ Stop; the old orchestrator keeps routing
    ↓ All pass
Promote orchestrator to production-v1
```

**Details that matter:**
- **Label entities in approved utterances.** CLU treats every unlabelled span as "not an entity", so importing approved questions bare would teach the model to stop extracting the very terms consultants use
- **Fail closed on metrics.** A missing `intentsEvaluation`, or a score outside 0–1 such as a percentage, blocks deployment instead of passing or reading as zero
- **Retrain the orchestrator after the child.** Connected intents copy the child's utterances at training time, so routing only learns from approved feedback when the orchestrator is retrained
- **Only the default branch promotes.** Other branches train in a separate `alira-transaction-advisory-ci` project and stop after staging, so a bad-data branch can prove the gate works without touching production
- **No overlapping runs.** A concurrency group stops imports and deployments from interleaving
- **Counts, not content, in CI logs.** Approved utterances can name confidential targets, and CI logs are widely readable

**What you'll learn:** Quality gates, regression replay, staging slots, model pruning, branch-safe CI for models.

---

## The Data Flow — End to End

### Flow 1: Consultant Screens M&A Targets

```
1. Consultant types "Show me oncology targets in Germany with revenue over 20M" in the portal
2. Portal sends POST /query {prompt}
3. Gateway:
   a. Rejects an empty prompt (400)
   b. Reads the current OpenTelemetry trace id and returns it as X-Trace-Id
   c. Runs as a mock user (no authentication in the sandbox build; DPIA risk R5)
4. Engine pseudonymises the user → user_hash (identity map row written once per process, in the background)
5. Azure CLU orchestrator (production-v1):
   a. Routes to Alira-MA-DueDiligence (confidence ≥ 0.5)
   b. Nested child prediction: ScreenTargets, TherapeuticArea="oncology", Geography="Germany", RevenueFloorUsd="20M"
6. Snowflake (key-pair auth):
   a. oncology → Oncology, Germany → DE (bound lookups in VOCABULARY_SYNONYMS)
   b. SELECT company_name, annual_revenue AS revenue, therapeutic_area, country
      FROM alira_dw.assistant.m_and_a_targets
      WHERE therapeutic_area = %s AND country = %s AND annual_revenue >= %s
      bound to ('Oncology', 'DE', 20000000.0)
7. Telemetry: QueryRouted {practice, intent=ScreenTargets, confidence, latency_ms, row_count=1, outcome=ROWS, user_hash}
8. Gateway returns {type: "MA_TARGETS", message: null, data: [Kura Therapeutics DE]}
9. Portal renders the targets table with a "Report wrong result" link
```

The same question in Teams arrives at `POST /api/messages`, runs through the same engine in a worker thread, and comes back as an Adaptive Card.

### Flow 2: Consultant Asks a Market Access Question

```
1. Consultant asks "What is the reimbursement status of Keytruda in France?"
2. POST /query → orchestrator routes to Alira-MarketAccess (no child model)
3. Engine calls foundry_orchestrator.ask_orchestrator with the prompt, trace_id, user_hash
   and a redacted copy of the prompt for the trace log
4. Foundry runs the latest version of alira-master-orchestrator-agent:
   a. market_access_analyst (Cortex Analyst) → SQL generated from market_access_model.yaml
   b. run_sql → executes that exact SQL through the MCP server, as the PAT's role
   c. The model writes a prose answer from the rows
5. Bridge reads the response:
   a. mcp_call items → tool names (market_access_analyst+run_sql) and failures
   b. Tool outputs → flat rows (the SQL result set is unpacked; Analyst's SQL text yields none)
   c. An mcp_approval_request → error, since this service has no approval step
6. Trace line appended to logs/orchestration_traces.txt (tool names, latency, redacted prompt)
7. Telemetry: QueryRouted {outcome=AGENT_ROWS, or AGENT_TEXT if no tool returned rows}
8. Gateway returns {type: "MARKET_ACCESS", message: agent prose, data: rows}
9. Portal shows the prose above a table whose columns come from the rows
```

RWE questions take the same path with `rwe_registry_search`. Its hits carry `@scores`, which become `scores_cosine_similarity` and `scores_reranker_score` columns.

### Flow 3: The Assistant Can't Map a Term

```
1. Consultant asks "Show me NASH targets in France"
2. Suppose the child model extracts TherapeuticArea="NASH", Geography="France"
3. "France" maps to FR; "NASH" has no row in VOCABULARY_SYNONYMS
4. Engine:
   a. Emits UnmappedTerm {detected_term (redacted), detected_category=THERAPEUTIC_AREA}
   b. Queues an NLU_FEEDBACK row: UNMAPPED_TERM, full question, the term, suggested intent ScreenTargets
   c. Skips the warehouse query and emits QueryRouted {outcome=UNMAPPED_TERM, row_count=0}
5. Portal shows "No matching targets found."
6. Steward opens NLU_FEEDBACK_REVIEW, maps "NASH" to the Hepatology / NASH master code, approves:
   a. VOCABULARY_SYNONYMS gains (THERAPEUTIC_AREA, "nash", "Hepatology / NASH")
      → the same question now returns Nantes Liver Biologics
   b. The row becomes APPROVED → next Monday's retrain adds it as a ScreenTargets example
```

The other triggers work the same way: a fallback to `None` queues `LOW_CONFIDENCE` (with the best-scoring intent as a suggestion when the child was unsure), and a query that ran but matched nothing queues `NO_ROWS`.

### Flow 4: Consultant Reports a Wrong Answer

```
1. The last answer looks wrong; the consultant clicks "Report wrong result"
2. Portal sends POST /feedback {trace_id (from X-Trace-Id), prompt}
3. Pydantic validates both fields (trace_id ≤ 64 chars, prompt ≤ 1,000) → 202 Accepted
4. Engine queues an NLU_FEEDBACK row: WRONG_RESULT, full prompt, trace_id
5. Steward reviews it and approves it with the intent the consultant meant, or rejects it
6. An engineer joins NLU_FEEDBACK.trace_id to customDimensions.trace_id in Application Insights
   → routing, confidence, outcome and latency for that exact request, without the user's identity
```

---

## File Structure — Where Everything Lives

```
alira_assistant_project/
├── main.py                          # FastAPI gateway: /query, /feedback, Teams endpoint, telemetry setup
├── snowflake_engine.py              # Business logic: CLU routing, synonyms → bound SQL, feedback capture
├── foundry_orchestrator.py          # Agent bridge: config validation, publish, ask, MCP result parsing
├── teams_bot.py                     # Teams front end: mention stripping, Adaptive Card tables
├── telemetry.py                     # HMAC pseudonymisation, fail-closed redaction, structured events
├── deploy_clu.py                    # First deployment: import, train, deploy child then orchestrator
│
├── agent_config.yaml                # Foundry agent: instructions, MCP server, tool allowlist
├── market_access_model.yaml         # Cortex Analyst semantic model for market access pricing
├── clu_orchestrator_dataset.json    # Orchestrator: practice intents and routing utterances
├── clu_base_dataset.json            # Child model seed set (alira-transaction-advisory)
├── clu_regression_set.json          # Cases replayed on staging before promotion
│
├── requirements.txt                 # Pinned Python dependencies
├── Dockerfile.dev                   # Out of date: references files that don't exist (see README)
├── .env                             # Local secrets (never commit)
├── Alira_Health_AI_Assistant_Blueprint (1).md   # This file
├── README.md                        # Quickstart, env vars, API, known gaps
│
├── test_main.py                     # Gateway: trace header, payload shape, feedback endpoint, startup guard
├── test_middleware.py               # Engine: screening, unmapped terms, zero rows, fallbacks, agent routing
├── test_foundry_orchestrator.py     # Agent: config validation, tool rows, trace log, approval handling
├── test_teams_bot.py                # Teams: revenue formatting, capped cards, text replies
├── test_telemetry.py                # Hashing, redaction, forbidden dimensions, customEvents export
├── test_retrain_clu.py              # Entity labelling, dataset merge, quality gate, pruning, smoke tests
├── test_monitoring.py               # KQL reads only emitted events; workbook matches the KQL files
│
├── scripts/
│   └── retrain_clu.py               # Weekly retraining: merge, train, gate, staging replay, promote
│
├── feedback_review_app/
│   └── nlu_feedback_review.py       # Streamlit-in-Snowflake review app for the data steward
│
├── sql/
│   └── phase5_observability.sql     # Feedback table, redaction list, identity store, roles, review app
│
├── monitoring/
│   ├── observability.bicep          # 30-day retention + NLU health workbook
│   ├── alira_nlu_workbook.json      # Workbook definition loaded by the Bicep template
│   └── kql/
│       ├── 01_unmapped_terms.kql
│       ├── 02_routing_confidence_by_practice.kql
│       └── 03_zero_row_queries.kql
│
├── frontend/                        # PRESENTATION TIER (React 19 + Vite 8)
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.jsx                 # React entry point
│       ├── App.jsx                  # Renders AliraDashboard
│       ├── AliraDashboard.jsx       # Market Intelligence Hub: prompt, result tables, wrong-result report
│       └── index.css
│
├── docs/
│   └── DPIA_assistant_telemetry.md  # Data inventory, controls, residual risks, sign-off
│
├── image/                           # Portal screenshots used in the README
├── logs/
│   └── orchestration_traces.txt     # Local agent traces (tool names, latency, redacted prompts)
│
└── .github/
    └── workflows/
        └── retrain_clu.yml          # Weekly metric-gated CLU retraining
```

---

## Learning Path — What Each Phase Teaches

| Phase | What You Build | What You Learn | Industry Relevance |
|-------|---------------|----------------|-------------------|
| 1 | Azure AI Language resource, Entra ID app registrations, Snowflake schema and warehouse ([Appendix](#appendix-one-time-cloud-setup)) | Cloud RBAC, OAuth scopes and app roles, warehouse sizing | Every enterprise cloud project |
| 2 | CLU orchestrator + child model, synonym table, bound SQL | Intent routing, entity normalisation, SQL-injection safety | Conversational AI |
| 3 | FastAPI gateway, React portal, Teams bot | API contracts, async vs. blocking I/O, Adaptive Cards | Backend + frontend engineering |
| 4 | Foundry prompt agent + Snowflake MCP (Cortex Analyst, Cortex Search) | Agentic tool use, allowlists, secret handling | AI agent architecture |
| 5.1 | Structured telemetry, KQL dashboards, retention in Bicep | Observability, drift monitoring, infrastructure as code | SRE / MLOps |
| 5.2 | HMAC pseudonymisation, fail-closed redaction, identity store, DPIA | Privacy engineering | Privacy / compliance |
| 5.3 | `NLU_FEEDBACK` and the Streamlit review app | Human-in-the-loop, active learning | Data governance |
| 5.5 | Weekly metric-gated retraining in GitHub Actions | Quality gates, regression replay, staging slots | MLOps |
| 6 | Pilot hardening (next) | Entra ID on the API, CI for tests and build, scripted Cortex objects | Production readiness |

Sub-phase numbers match the comments in the code (`telemetry.py`, `sql/phase5_observability.sql`, `scripts/retrain_clu.py`).

---

## Key Concepts You'll Master

1. **CLU Orchestration** — A parent project that routes each question to the right practice
2. **Connected Intents** — The orchestrator calls a child deployment and nests its prediction
3. **Confidence Thresholds and Fallbacks** — "I didn't understand" beats a wrong query
4. **Entity Normalisation** — A synonym table between what people say and what the data stores
5. **Parameter Binding** — User text never becomes SQL
6. **Failing Safely on Unknown Input** — An unmapped term skips the query and asks a human
7. **Prompt Agents** — Versioned agent definitions in Azure AI Foundry
8. **Model Context Protocol (MCP)** — A standard tool interface, restricted by an allowlist
9. **Cortex Analyst** — A semantic model that turns questions into SQL
10. **Cortex Search** — Warehouse-native search with similarity and reranker scores
11. **Secrets Outside Agent Definitions** — Project connections instead of tokens in config
12. **One Response Contract** — `{type, message, data}` for every engine and every UI
13. **Structured Custom Events** — Shared dimensions every dashboard can slice by
14. **Drift Monitoring with KQL** — Confidence percentiles, unmapped terms, zero-row answers
15. **Keyed Pseudonymisation** — HMAC user hashes with a separately guarded reverse map
16. **Fail-Closed Redaction** — Withhold text until the redaction list has loaded
17. **Managed-Access Schemas** — Only the schema owner can grant on re-identification data
18. **Human-in-the-Loop Active Learning** — Captured misses, approved by a named steward
19. **Entity Labelling for Imports** — Unlabelled spans teach CLU "not an entity"
20. **Quality Gates** — Macro F1 and precision thresholds that block deployment
21. **Regression Replay on Staging** — Known cases must pass before promotion
22. **Branch-Safe Model CI** — Only the default branch promotes to production
23. **Retention as Code** — Bicep-managed log retention with no archive tier
24. **DPIA from Implementation** — Privacy documentation derived from what the code actually does

---

## Production Scaling Notes (For Your Resume/Interviews)

This project is a **personal sandbox** with mock data. On the way to production:

- **Mock user → Entra ID:** validate the `Data.Query` scope on `/query` and `/feedback` and pass the real `oid`. The DPIA marks this blocking for a pilot (R5)
- **`allow_origins=["*"]` → the portal's origin only**, and the hardcoded `http://localhost:8000` → build-time configuration
- **A new Snowflake connection per call → a connection pool:** `snowflake_cursor` opens a fresh connection for every screening request, and another for each feedback or identity-map write
- **In-process background writes → a durable queue:** feedback still queued in the thread pool is lost if the process is killed or scaled in
- **Local trace file → Application Insights:** `logs/orchestration_traces.txt` belongs to one instance and disappears with it
- **`TELEMETRY_HMAC_SECRET` in `.env` → Key Vault**, readable only by the API's managed identity (R3)
- **`QUERY_TAG` with the user id → `user_hash`** (R6)
- **Unbounded `NLU_FEEDBACK` and `USER_HASH_MAP` → scheduled deletion** once retention is decided (R7)
- **Hand-built Cortex objects and MCP server → scripted DDL** alongside `sql/phase5_observability.sql`
- **Retraining-only CI → a test-and-build workflow** that runs the 76 pytest checks and `npm run build` on every pull request
- **Local uvicorn → Azure App Service** with a managed identity, which `DefaultAzureCredential` and the connection-string handling already expect
- **Mock warehouse data → real tables** with row access policies by practice (R4)

---

## Cost Drivers

Everything runs on trial or pay-as-you-go accounts. These are the meters that matter, and the choices in this repo that keep them down:

| Component | What drives cost | How this repo keeps it down |
|-----------|-----------------|-----------------------------|
| Snowflake warehouse | Compute time. A SMALL warehouse bills 2 credits per hour per running cluster | `AUTO_SUSPEND = 60`, `SCALING_POLICY = 'ECONOMY'`, at most 5 clusters |
| Snowflake Cortex | Cortex Analyst requests; Cortex Search serving and index refresh | Only market access and RWE questions reach the agent |
| Azure AI Language | Prediction calls per question; training time per run | Weekly rather than daily retraining; old models pruned |
| Azure AI Foundry | Model tokens per agent call, including tool results fed back to the model | M&A questions never call a generative model |
| Application Insights | Data ingested and retained | Structured events instead of verbose logs; 30-day retention, no archive |
| GitHub Actions | Runner minutes (training is polled every 30 s, up to 180 min) | Weekly schedule; pull request runs only when datasets or the gate change |
| FastAPI, portal, Teams bot | Hosting plan once deployed | One app serves the portal API and Teams |

---

## Appendix: One-Time Cloud Setup

The [README Quickstart](README.md#quickstart) points here for Steps 1–3. Run the rest of the Quickstart afterwards.

### Step 1: Provision Azure AI Language

1. In the Azure portal, create an **Azure AI Language** resource.
2. Enable **Custom features** during creation. This provisions what custom CLU and orchestration projects need.
3. Assign these roles to the development team and the CI/CD service principal:
   * **Cognitive Services Language Owner/Contributor:** train and deploy models in Language Studio / Azure AI Foundry.
   * **Storage Blob Data Contributor:** CLU stores training data in the resource's underlying storage account.

### Step 2: Configure Microsoft Entra ID App Registrations

> **Status:** designed, not yet enforced. `/query` and `/feedback` still run as a mock user (DPIA R5).

1. Create a registration named **`Alira-Assistant-API`** (backend).
2. Under **Expose an API**, set the Application ID URI (`api://<client-id>`) and add a delegated scope named `Data.Query`.
3. Under **App roles**, define:
   * `Consultant` (value `Consultant`)
   * `MA_Advisory_Lead` (value `MA_Advisory_Lead`)
4. Create a second registration named **`Alira-Assistant-Client`** (frontend).
5. Under **API permissions**, choose *Add a permission* → *My APIs* → `Alira-Assistant-API`, tick `Data.Query`, and click **Grant admin consent**.

The Teams bot uses its own bot registration. Its `CLIENT_ID`, `CLIENT_SECRET` and `TENANT_ID` go in `.env` (see the README).

### Step 3: Initialize the Snowflake Data & Synonym Schemas

1. Create the schema, the core tables and the mock data. The code queries the **`ASSISTANT`** schema:
```sql
CREATE DATABASE IF NOT EXISTS ALIRA_DW;
CREATE SCHEMA IF NOT EXISTS ALIRA_DW.ASSISTANT;

-- Transaction targets the M&A screener queries
CREATE OR REPLACE TABLE ALIRA_DW.ASSISTANT.M_AND_A_TARGETS (
    target_id INT IDENTITY(1,1),
    company_name VARCHAR(150),
    therapeutic_area VARCHAR(100),
    country VARCHAR(10),            -- ISO country codes
    annual_revenue NUMBER(15,2),
    pipeline_stage VARCHAR(50),
    asset_type VARCHAR(50)
);

INSERT INTO ALIRA_DW.ASSISTANT.M_AND_A_TARGETS (company_name, therapeutic_area, country, annual_revenue, pipeline_stage, asset_type) VALUES
    ('Kura Therapeutics DE', 'Oncology', 'DE', 24500000.00, 'Phase II', 'Therapeutics'),
    ('Heidelberg MedTech', 'Oncology', 'DE', 8500000.00, 'Commercial', 'MedTech'),
    ('Nantes Liver Biologics', 'Hepatology / NASH', 'FR', 12000000.00, 'Phase I', 'Therapeutics'),
    ('Paris Digital Therapeutics', 'Neurology', 'FR', 3100000.00, 'Commercial', 'Digital Health'),
    ('Berlin CardioVascular', 'Cardiology', 'DE', 45000000.00, 'Phase III', 'Therapeutics'),
    ('Lyon Diagnostic Systems', 'Oncology', 'FR', 19500000.00, 'Commercial', 'MedTech'),
    ('Munich Orphan Pharma', 'Rare Disease', 'DE', 62000000.00, 'Phase III', 'Therapeutics'),
    ('London Oncology Group', 'Oncology', 'UK', 78000000.00, 'Commercial', 'Therapeutics');

-- Consultant vocabulary → master codes
CREATE OR REPLACE TABLE ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS (
    category VARCHAR(50),
    user_input VARCHAR(100),
    master_code VARCHAR(100)
);

INSERT INTO ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS (category, user_input, master_code) VALUES
    ('THERAPEUTIC_AREA', 'oncology', 'Oncology'),
    ('THERAPEUTIC_AREA', 'cancer', 'Oncology'),
    ('THERAPEUTIC_AREA', 'solid tumors', 'Oncology'),
    ('GEOGRAPHY', 'germany', 'DE'),
    ('GEOGRAPHY', 'de', 'DE'),
    ('GEOGRAPHY', 'france', 'FR');
```
2. Create an auto-scaling warehouse that suspends when idle:
```sql
CREATE OR REPLACE WAREHOUSE ALIRA_BOT_WH WITH
  WAREHOUSE_SIZE = 'SMALL'
  MIN_CLUSTER_COUNT = 1
  MAX_CLUSTER_COUNT = 5
  SCALING_POLICY = 'ECONOMY'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE;
```
3. Run [`sql/phase5_observability.sql`](sql/phase5_observability.sql) for the feedback table, redaction watchlist, identity store, roles and the review app.
4. Set up key-pair authentication for the API's service user, and point `SNOWFLAKE_PRIVATE_KEY_FILE` at the private key.

### Step 4: Azure AI Foundry + Snowflake Cortex (manual)

These objects are not scripted yet:

1. **Cortex Analyst:** upload [`market_access_model.yaml`](market_access_model.yaml). Its table is `ALIRA_DW.PROD.MARKET_ACCESS_REIMBURSEMENT`.
2. **Cortex Search:** create the registry cohort search service with the columns `cohort_name`, `biomarker_status`, `country` and `patient_count`.
3. **MCP server:** create `ALIRA_DW.ASSISTANT.ALIRA_INTELLIGENCE_MCP_SERVER` exposing `market_access_analyst`, `rwe_registry_search` and `run_sql`. The names must match `Allowed_Tools` in [`agent_config.yaml`](agent_config.yaml).
4. **PAT:** create a Snowflake programmatic access token for a role that can read only what the agent should see.
5. **Foundry project:** deploy a model, then add a project connection holding the header `Authorization: Bearer <Snowflake PAT>`. Put the model deployment name and connection name in `.env`.
6. **Monitoring** (optional): `az deployment group create --template-file monitoring/observability.bicep ...`

---

*Current state: Phases 1–5 built, covered by 76 deterministic tests with Azure, Snowflake and Foundry mocked. Next milestone: Phase 6, starting with Entra ID on `/query` and `/feedback`.*
