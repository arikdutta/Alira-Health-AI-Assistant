from decimal import Decimal
from unittest.mock import patch

from microsoft_teams.cards import AdaptiveCard

from teams_bot import MAX_CARD_ROWS, answer_query, format_revenue

MOCK_ROWS = [
    {"COMPANY_NAME": f"Target {i}", "REVENUE": Decimal("24500000.00"), "THERAPEUTIC_AREA": "Oncology", "COUNTRY": "DE"}
    for i in range(7)
]


def table_rows(card: AdaptiveCard) -> list[list[str]]:
    table = card.model_dump(by_alias=True, exclude_none=True)["body"][2]
    return [[cell["items"][0]["text"] for cell in row["cells"]] for row in table["rows"]]


def test_format_revenue_handles_snowflake_decimals():
    assert format_revenue(Decimal("24500000.00")) == "$24.5M"
    assert format_revenue(None) == "n/a"


@patch("teams_bot.query_alira_assistant", return_value=MOCK_ROWS)
def test_rows_become_a_capped_table_card(mock_query):
    card = answer_query("Show me oncology targets in Germany", "aad-user-1")

    assert isinstance(card, AdaptiveCard)
    rows = table_rows(card)
    assert rows[0] == ["Company", "Area", "Country", "Revenue"]
    assert rows[1] == ["Target 0", "Oncology", "DE", "$24.5M"]
    assert len(rows) == MAX_CARD_ROWS + 1
    assert card.body[1].text == f"Showing {MAX_CARD_ROWS} of {len(MOCK_ROWS)} matches"
    mock_query.assert_called_once_with("Show me oncology targets in Germany", {"preferred_username": "aad-user-1"})


@patch("teams_bot.query_alira_assistant", return_value={"message": "Intent None/None caught but no SQL mapping exists."})
def test_unmapped_intent_message_is_sent_as_text(_):
    assert answer_query("What's the weather?", "aad-user-1") == "Intent None/None caught but no SQL mapping exists."


@patch("teams_bot.query_alira_assistant", return_value=[])
def test_empty_result_says_no_matches(_):
    assert answer_query("Show me cardiology targets in Japan", "aad-user-1") == "No matching targets found."
