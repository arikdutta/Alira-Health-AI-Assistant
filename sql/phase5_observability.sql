-- Phase 5: feedback loop, telemetry redaction source and pseudonymisation identity store.
-- Safe to re-run: tables use IF NOT EXISTS so the steward's review backlog survives a redeploy.
-- Role names below are placeholders for your RBAC model; the privilege split between them is the point.

USE DATABASE ALIRA_DW;

------------------------------------------------------------------------------------------------------
-- 5.3 Feedback captured by the assistant, reviewed by the data steward
------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ALIRA_DW.ASSISTANT.NLU_FEEDBACK (
    feedback_id        NUMBER IDENTITY(1,1),
    trace_id           VARCHAR(64),
    raw_utterance      VARCHAR(1000),
    failure_type       VARCHAR(30),    -- LOW_CONFIDENCE | UNMAPPED_TERM | WRONG_RESULT | NO_ROWS
    detected_term      VARCHAR(200),
    detected_category  VARCHAR(50),
    suggested_intent   VARCHAR(100),
    captured_at        TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    review_status      VARCHAR(20) DEFAULT 'PENDING',    -- PENDING | APPROVED | REJECTED
    reviewed_by        VARCHAR(100),
    reviewed_at        TIMESTAMP_NTZ,
    -- Set when the steward resolves an UNMAPPED_TERM by adding it to VOCABULARY_SYNONYMS
    mapped_master_code VARCHAR(100)
);

-- The retraining pipeline reads ONLY human-approved rows.
CREATE OR REPLACE VIEW ALIRA_DW.ASSISTANT.V_APPROVED_TRAINING_DATA AS
SELECT raw_utterance, suggested_intent
FROM ALIRA_DW.ASSISTANT.NLU_FEEDBACK
WHERE review_status = 'APPROVED';

------------------------------------------------------------------------------------------------------
-- 5.2 Company-name master list that telemetry redaction strips from utterances
------------------------------------------------------------------------------------------------------
-- Live or confidential targets are often discussed before they exist in M_AND_A_TARGETS.
-- Deal teams add those names (and project codenames) here; the API picks them up within an hour.
CREATE TABLE IF NOT EXISTS ALIRA_DW.ASSISTANT.REDACTION_WATCHLIST (
    term       VARCHAR(200) NOT NULL,
    added_by   VARCHAR(100) DEFAULT CURRENT_USER(),
    added_at   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE OR REPLACE VIEW ALIRA_DW.ASSISTANT.V_COMPANY_NAME_MASTER AS
SELECT DISTINCT company_name
FROM (
    SELECT company_name FROM ALIRA_DW.ASSISTANT.M_AND_A_TARGETS
    UNION ALL
    SELECT term FROM ALIRA_DW.ASSISTANT.REDACTION_WATCHLIST
)
WHERE company_name IS NOT NULL;

------------------------------------------------------------------------------------------------------
-- 5.2 Reverse mapping user_hash -> Entra object id, separate from the analytics data
------------------------------------------------------------------------------------------------------
-- Managed access: only the schema owner can grant on these objects, not individual table owners.
CREATE SCHEMA IF NOT EXISTS ALIRA_DW.TELEMETRY_IDENTITY WITH MANAGED ACCESS;

-- Append-only: the API may insert but never read. Duplicate rows across restarts are expected.
CREATE TABLE IF NOT EXISTS ALIRA_DW.TELEMETRY_IDENTITY.USER_HASH_MAP (
    user_hash   VARCHAR(64) NOT NULL,
    oid         VARCHAR(100) NOT NULL,
    recorded_at TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
);

------------------------------------------------------------------------------------------------------
-- Roles
------------------------------------------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS ALIRA_ASSISTANT_API;     -- the FastAPI / Teams service user
CREATE ROLE IF NOT EXISTS ALIRA_FEEDBACK_APP;      -- owns the review app; the app runs with this role's rights
CREATE ROLE IF NOT EXISTS ALIRA_DATA_STEWARD;      -- the named senior consultant who reviews feedback
CREATE ROLE IF NOT EXISTS ALIRA_CLU_RETRAIN;       -- the GitHub Actions retraining job
CREATE ROLE IF NOT EXISTS ALIRA_PRIVACY_OFFICER;   -- the only role that can re-identify a user_hash

GRANT USAGE ON DATABASE ALIRA_DW TO ROLE ALIRA_ASSISTANT_API;
GRANT USAGE ON DATABASE ALIRA_DW TO ROLE ALIRA_FEEDBACK_APP;
GRANT USAGE ON DATABASE ALIRA_DW TO ROLE ALIRA_DATA_STEWARD;
GRANT USAGE ON DATABASE ALIRA_DW TO ROLE ALIRA_CLU_RETRAIN;
GRANT USAGE ON DATABASE ALIRA_DW TO ROLE ALIRA_PRIVACY_OFFICER;
GRANT USAGE ON SCHEMA ALIRA_DW.ASSISTANT TO ROLE ALIRA_ASSISTANT_API;
GRANT USAGE ON SCHEMA ALIRA_DW.ASSISTANT TO ROLE ALIRA_FEEDBACK_APP;
GRANT USAGE ON SCHEMA ALIRA_DW.ASSISTANT TO ROLE ALIRA_DATA_STEWARD;
GRANT USAGE ON SCHEMA ALIRA_DW.ASSISTANT TO ROLE ALIRA_CLU_RETRAIN;

-- API: run queries, write feedback, read the redaction list, append to the identity map
GRANT SELECT ON TABLE ALIRA_DW.ASSISTANT.M_AND_A_TARGETS TO ROLE ALIRA_ASSISTANT_API;
GRANT SELECT ON TABLE ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS TO ROLE ALIRA_ASSISTANT_API;
GRANT INSERT ON TABLE ALIRA_DW.ASSISTANT.NLU_FEEDBACK TO ROLE ALIRA_ASSISTANT_API;
GRANT SELECT ON VIEW ALIRA_DW.ASSISTANT.V_COMPANY_NAME_MASTER TO ROLE ALIRA_ASSISTANT_API;
GRANT USAGE ON SCHEMA ALIRA_DW.TELEMETRY_IDENTITY TO ROLE ALIRA_ASSISTANT_API;
GRANT INSERT ON TABLE ALIRA_DW.TELEMETRY_IDENTITY.USER_HASH_MAP TO ROLE ALIRA_ASSISTANT_API;

-- Review app: approve/reject feedback and add synonyms, so an unmapped term is fixed without an engineer
GRANT SELECT, UPDATE ON TABLE ALIRA_DW.ASSISTANT.NLU_FEEDBACK TO ROLE ALIRA_FEEDBACK_APP;
GRANT SELECT, INSERT ON TABLE ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS TO ROLE ALIRA_FEEDBACK_APP;
GRANT CREATE STREAMLIT, CREATE STAGE ON SCHEMA ALIRA_DW.ASSISTANT TO ROLE ALIRA_FEEDBACK_APP;
GRANT USAGE ON WAREHOUSE ALIRA_BOT_WH TO ROLE ALIRA_FEEDBACK_APP;

-- Retraining: approved utterances plus the vocabulary used to label entities in them
GRANT SELECT ON VIEW ALIRA_DW.ASSISTANT.V_APPROVED_TRAINING_DATA TO ROLE ALIRA_CLU_RETRAIN;
GRANT SELECT ON TABLE ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS TO ROLE ALIRA_CLU_RETRAIN;
GRANT USAGE ON WAREHOUSE ALIRA_BOT_WH TO ROLE ALIRA_CLU_RETRAIN;

-- Re-identification is a privacy-officer action, never an engineering one
GRANT USAGE ON SCHEMA ALIRA_DW.TELEMETRY_IDENTITY TO ROLE ALIRA_PRIVACY_OFFICER;
GRANT SELECT ON TABLE ALIRA_DW.TELEMETRY_IDENTITY.USER_HASH_MAP TO ROLE ALIRA_PRIVACY_OFFICER;
GRANT USAGE ON WAREHOUSE ALIRA_BOT_WH TO ROLE ALIRA_PRIVACY_OFFICER;

-- GRANT ROLE ALIRA_ASSISTANT_API   TO USER <api_service_user>;
-- GRANT ROLE ALIRA_CLU_RETRAIN     TO USER <github_actions_service_user>;
-- GRANT ROLE ALIRA_DATA_STEWARD    TO USER <named_data_steward>;
-- GRANT ROLE ALIRA_PRIVACY_OFFICER TO USER <dpo_user>;

------------------------------------------------------------------------------------------------------
-- 5.3 Streamlit-in-Snowflake review app
------------------------------------------------------------------------------------------------------
-- Deliberately not done for ALIRA_PRIVACY_OFFICER: SYSADMIN inheriting it would defeat the separation
GRANT ROLE ALIRA_FEEDBACK_APP TO ROLE SYSADMIN;
USE ROLE ALIRA_FEEDBACK_APP;

CREATE STAGE IF NOT EXISTS ALIRA_DW.ASSISTANT.STREAMLIT_APPS DIRECTORY = (ENABLE = TRUE);

-- Upload from the repo root (SnowSQL or `snow stage copy`), then create the app:
-- PUT file://feedback_review_app/nlu_feedback_review.py @ALIRA_DW.ASSISTANT.STREAMLIT_APPS/nlu_feedback_review/ AUTO_COMPRESS=FALSE OVERWRITE=TRUE;

CREATE OR REPLACE STREAMLIT ALIRA_DW.ASSISTANT.NLU_FEEDBACK_REVIEW
    FROM '@ALIRA_DW.ASSISTANT.STREAMLIT_APPS/nlu_feedback_review'
    MAIN_FILE = 'nlu_feedback_review.py'
    QUERY_WAREHOUSE = ALIRA_BOT_WH
    TITLE = 'Alira assistant - NLU feedback review';

-- The app runs with ALIRA_FEEDBACK_APP's rights, so access is controlled by who may open it
GRANT USAGE ON STREAMLIT ALIRA_DW.ASSISTANT.NLU_FEEDBACK_REVIEW TO ROLE ALIRA_DATA_STEWARD;
