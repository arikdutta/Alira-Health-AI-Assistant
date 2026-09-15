"""Microsoft Teams front end for the assistant, served from the same FastAPI app as /query."""
import asyncio
import logging

from fastapi import FastAPI
from microsoft_teams.api import MessageActivity, TypingActivityInput
from microsoft_teams.api.activities.utils import StripMentionsTextOptions
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
from microsoft_teams.cards import AdaptiveCard, ColumnDefinition, Table, TableCell, TableRow, TextBlock

from snowflake_engine import query_alira_assistant

logger = logging.getLogger(__name__)

# Tables get cramped past a handful of rows, especially in the Teams mobile client
MAX_CARD_ROWS = 5
EXAMPLE_PROMPT = "Show me oncology targets in Germany with revenue over 20M"


def format_revenue(value) -> str:
    # Snowflake returns NUMBER columns as Decimal
    return "n/a" if value is None else f"${float(value) / 1_000_000:,.1f}M"


def _cell(text: str, **text_options) -> TableCell:
    return TableCell(items=[TextBlock(text=text, wrap=True, **text_options)])


def build_results_card(rows: list[dict]) -> AdaptiveCard:
    shown = rows[:MAX_CARD_ROWS]
    header = TableRow(cells=[
        _cell("Company", weight="Bolder"),
        _cell("Area", weight="Bolder"),
        _cell("Country", weight="Bolder"),
        _cell("Revenue", weight="Bolder", horizontal_alignment="Right"),
    ])
    records = [
        TableRow(cells=[
            _cell(str(row.get("COMPANY_NAME", "Unknown"))),
            _cell(str(row.get("THERAPEUTIC_AREA", "Unknown"))),
            _cell(str(row.get("COUNTRY", "Unknown"))),
            _cell(format_revenue(row.get("REVENUE")), horizontal_alignment="Right"),
        ])
        for row in shown
    ]
    return AdaptiveCard(body=[
        TextBlock(text="🎯 M&A Target Screening Results", weight="Bolder", size="Medium", wrap=True),
        TextBlock(
            text=f"Showing {len(shown)} of {len(rows)} {'match' if len(rows) == 1 else 'matches'}",
            is_subtle=True, spacing="None", wrap=True,
        ),
        Table(
            columns=[ColumnDefinition(width=3), ColumnDefinition(width=2), ColumnDefinition(width=1), ColumnDefinition(width=2)],
            rows=[header, *records],
        ),
    ])


def answer_query(prompt: str, user_id: str) -> str | AdaptiveCard:
    """Runs a prompt through the CLU + Snowflake engine and shapes the result for Teams."""
    result = query_alira_assistant(prompt, {"preferred_username": user_id})
    # The engine returns {"message": ...} instead of rows when it can't answer, and for Foundry agent answers,
    # whose text already summarises any rows the agent's tools returned
    if isinstance(result, dict):
        return result.get("message", "I couldn't map that question to a data query.")
    if not result:
        return "No matching targets found."
    return build_results_card(result)


def create_teams_app(fastapi_app: FastAPI) -> App:
    """Attaches the Teams messaging endpoint (POST /api/messages) to an existing FastAPI app.

    Call `await teams_app.initialize()` at startup to register the route.
    """
    teams_app = App(http_server_adapter=FastAPIAdapter(app=fastapi_app))

    @teams_app.on_message
    async def handle_message(ctx: ActivityContext[MessageActivity]):
        # In channels and group chats the text starts with "<at>BotName</at>"; drop only the bot's own mention
        prompt = (ctx.activity.strip_mentions_text(StripMentionsTextOptions(account_id=ctx.activity.recipient.id)).text or "").strip()
        if not prompt:
            await ctx.send(f"Ask me about M&A targets, for example: *{EXAMPLE_PROMPT}*")
            return

        await ctx.send(TypingActivityInput())
        sender = ctx.activity.from_
        try:
            # The engine makes blocking Azure and Snowflake calls, so keep them off the event loop
            reply = await asyncio.to_thread(answer_query, prompt, sender.aad_object_id or sender.id)
        except Exception:
            logger.exception("Teams query failed")
            reply = "Sorry, something went wrong while querying the data warehouse. Please try again."
        await ctx.send(reply)

    return teams_app
