"""NLU feedback review - Streamlit in Snowflake app for the assistant's data steward.

Approving a row makes its utterance a training example for the weekly CLU retraining run (only APPROVED rows
reach V_APPROVED_TRAINING_DATA). Approving an UNMAPPED_TERM row also adds the term to VOCABULARY_SYNONYMS,
which fixes the consultant's query immediately, without an engineer or a retrain.

Deployed by sql/phase5_observability.sql. Runs with the ALIRA_FEEDBACK_APP role's rights.
"""
import streamlit as st
from snowflake.snowpark.context import get_active_session

FEEDBACK_TABLE = "ALIRA_DW.ASSISTANT.NLU_FEEDBACK"
SYNONYMS_TABLE = "ALIRA_DW.ASSISTANT.VOCABULARY_SYNONYMS"

# Intents of the alira-transaction-advisory child project; keep in step with clu_base_dataset.json
INTENTS = ["ScreenTargets", "GetDealComparables", "None"]
FAILURE_TYPES = ["UNMAPPED_TERM", "LOW_CONFIDENCE", "NO_ROWS", "WRONG_RESULT"]
FAILURE_HELP = {
    "UNMAPPED_TERM": "A filter word had no synonym. Map it to a master code to fix the query for everyone.",
    "LOW_CONFIDENCE": "The model wasn't sure what was asked. Approve with the intent the consultant meant.",
    "NO_ROWS": "The query ran but returned nothing. Reject if the data simply doesn't exist.",
    "WRONG_RESULT": "A consultant reported the answer as wrong. Approve with the intent they meant.",
}
NEW_CODE_OPTION = "New master code..."
PAGE_SIZE = 20

st.set_page_config(page_title="NLU feedback review", layout="wide")
session = get_active_session()


def sql(query: str, params=None):
    return session.sql(query, params=params or []).collect()


def reviewer_name() -> str:
    # CURRENT_USER() would return the app owner here, since the app runs with owner's rights
    user = getattr(st, "user", None) or getattr(st, "experimental_user", None)
    try:
        return user["user_name"]
    except (KeyError, TypeError):
        return ""


def master_codes(category: str) -> list[str]:
    rows = sql(f"SELECT DISTINCT master_code FROM {SYNONYMS_TABLE} WHERE category = ? ORDER BY 1", [category])
    return [row["MASTER_CODE"] for row in rows]


def approve(row, intent: str, master_code, reviewer: str) -> None:
    if row["FAILURE_TYPE"] == "UNMAPPED_TERM":
        sql(
            f"""INSERT INTO {SYNONYMS_TABLE} (category, user_input, master_code)
                SELECT ?, LOWER(?), ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM {SYNONYMS_TABLE} WHERE category = ? AND LOWER(user_input) = LOWER(?)
                )""",
            [row["DETECTED_CATEGORY"], row["DETECTED_TERM"], master_code, row["DETECTED_CATEGORY"], row["DETECTED_TERM"]],
        )
    sql(
        f"""UPDATE {FEEDBACK_TABLE}
            SET review_status = 'APPROVED', suggested_intent = ?, mapped_master_code = ?,
                reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP()
            WHERE feedback_id = ? AND review_status = 'PENDING'""",
        [intent, master_code, reviewer, row["FEEDBACK_ID"]],
    )


def reject(row, reviewer: str) -> None:
    sql(
        f"""UPDATE {FEEDBACK_TABLE}
            SET review_status = 'REJECTED', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP()
            WHERE feedback_id = ? AND review_status = 'PENDING'""",
        [reviewer, row["FEEDBACK_ID"]],
    )


def review_form(row, reviewer: str) -> None:
    failure_type = row["FAILURE_TYPE"]
    with st.form(key=f"feedback-{row['FEEDBACK_ID']}"):
        intent_options = ["Choose an intent..."] + INTENTS
        suggested = row["SUGGESTED_INTENT"]
        intent = st.selectbox(
            "Intent the consultant meant",
            intent_options,
            index=intent_options.index(suggested) if suggested in INTENTS else 0,
        )

        master_code = None
        new_code = ""
        if failure_type == "UNMAPPED_TERM":
            code_options = master_codes(row["DETECTED_CATEGORY"]) + [NEW_CODE_OPTION]
            master_code = st.selectbox(f"\"{row['DETECTED_TERM']}\" means", code_options)
            new_code = st.text_input("New master code (only if not listed above)").strip()

        approve_clicked, reject_clicked = st.columns(2)
        approved = approve_clicked.form_submit_button("Approve", type="primary", use_container_width=True)
        rejected = reject_clicked.form_submit_button("Reject", use_container_width=True)

    if approved:
        if intent not in INTENTS:
            st.error("Choose the intent before approving.")
            return
        if failure_type == "UNMAPPED_TERM":
            master_code = new_code if master_code == NEW_CODE_OPTION else master_code
            if not master_code:
                st.error("Choose or enter the master code this term maps to.")
                return
        approve(row, intent, master_code, reviewer)
        st.toast(f"Approved feedback #{row['FEEDBACK_ID']}")
        st.rerun()
    if rejected:
        reject(row, reviewer)
        st.toast(f"Rejected feedback #{row['FEEDBACK_ID']}")
        st.rerun()


st.title("NLU feedback review")
st.caption(
    "Utterances the assistant struggled with. Approved rows train next Monday's model; "
    "mapped terms work in the assistant straight away. Utterances may name confidential targets - do not copy them out."
)

reviewer = reviewer_name()
if not reviewer:
    st.error("Couldn't identify your Snowflake user, so reviews can't be attributed. Contact the platform team.")
    st.stop()

counts = {row["FAILURE_TYPE"]: row["N"] for row in sql(
    f"SELECT failure_type, COUNT(*) AS n FROM {FEEDBACK_TABLE} WHERE review_status = 'PENDING' GROUP BY failure_type"
)}
for column, failure_type in zip(st.columns(len(FAILURE_TYPES)), FAILURE_TYPES):
    column.metric(failure_type.replace("_", " ").title(), counts.get(failure_type, 0))

with st.sidebar:
    status = st.radio("Status", ["PENDING", "APPROVED", "REJECTED"])
    selected_types = st.multiselect("Failure type", FAILURE_TYPES, default=FAILURE_TYPES)

if not selected_types:
    st.info("Select at least one failure type.")
    st.stop()

placeholders = ", ".join("?" for _ in selected_types)
rows = sql(
    f"""SELECT feedback_id, raw_utterance, failure_type, detected_term, detected_category, suggested_intent,
               captured_at, reviewed_by, reviewed_at, mapped_master_code
        FROM {FEEDBACK_TABLE}
        WHERE review_status = ? AND failure_type IN ({placeholders})
        ORDER BY captured_at {'ASC' if status == 'PENDING' else 'DESC'}
        LIMIT {PAGE_SIZE}""",
    [status, *selected_types],
)

if not rows:
    st.success("Nothing to review." if status == "PENDING" else "No rows.")

for row in rows:
    with st.container(border=True):
        st.markdown(f"**{row['RAW_UTTERANCE']}**")
        details = [row["FAILURE_TYPE"], f"captured {row['CAPTURED_AT']:%Y-%m-%d %H:%M}"]
        if row["DETECTED_TERM"]:
            details.append(f"term \"{row['DETECTED_TERM']}\" ({row['DETECTED_CATEGORY']})")
        st.caption(" · ".join(details))

        if status == "PENDING":
            st.caption(FAILURE_HELP[row["FAILURE_TYPE"]])
            review_form(row, reviewer)
        else:
            mapped = f", mapped to {row['MAPPED_MASTER_CODE']}" if row["MAPPED_MASTER_CODE"] else ""
            st.write(f"{status.title()} by {row['REVIEWED_BY']} on {row['REVIEWED_AT']:%Y-%m-%d}"
                     f" - intent {row['SUGGESTED_INTENT']}{mapped}")
