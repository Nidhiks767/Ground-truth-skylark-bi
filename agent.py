"""
The BI agent itself: a tool-use loop that can pull live data from the two
monday.com boards (Deals, Work Orders), reason over it, and answer
founder-level business questions.

Uses Groq (OpenAI-compatible API, free tier, no credit card required) as the
reasoning engine, running an open-weight model that supports function/tool
calling. See MODEL below - Groq deprecates/renames models periodically;
check https://console.groq.com/docs/models if this stops working.
"""
import os
import json
from datetime import date
from openai import OpenAI  # Groq exposes an OpenAI-compatible API

from monday_client import get_board_items, MondayClientError
from data_shaping import summarize_rows, filter_rows, normalize_dates_in_rows, find_unmatched

MODEL = "openai/gpt-oss-120b"

DEALS_DATE_FIELDS = ["Close Date (A)", "Tentative Close Date", "Created Date"]
WORK_ORDERS_DATE_FIELDS = [
    "Data Delivery Date", "Date of PO/LOI", "Probable Start Date",
    "Probable End Date", "Last invoice date", "Collection Date",
]


def get_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["GROQ_API_KEY"],
        base_url="https://api.groq.com/openai/v1",
        timeout=25.0,     # fail fast instead of hanging - the SDK's own
        max_retries=1,    # default (long backoff, multiple retries) can turn
                           # one slow call into a multi-minute silent wait
    )

DEALS_BOARD_ID = os.environ.get("MONDAY_DEALS_BOARD_ID")
WORK_ORDERS_BOARD_ID = os.environ.get("MONDAY_WORK_ORDERS_BOARD_ID")

_cache: dict[str, list[dict]] = {}
_date_norm_stats: dict[str, dict] = {}


def _cached_board(board_id: str, cache_key: str) -> list[dict]:
    """Very small in-memory cache so a multi-turn chat doesn't re-fetch
    the whole board on every message. Cleared per process restart."""
    if cache_key not in _cache:
        _cache[cache_key] = get_board_items(board_id)
    return _cache[cache_key]


def _cached_normalized_board(board_id: str, cache_key: str, date_fields: list[str]) -> list[dict]:
    """Same as _cached_board, but with known date-like fields rewritten to
    a single canonical 'YYYY-MM-DD' form (see data_shaping.normalize_date).
    Normalization stats are cached alongside so summary tools can report
    them without re-computing."""
    norm_key = f"{cache_key}_normalized"
    if norm_key not in _cache:
        raw = _cached_board(board_id, cache_key)
        normalized, stats = normalize_dates_in_rows(raw, date_fields)
        _cache[norm_key] = normalized
        _date_norm_stats[cache_key] = stats
    return _cache[norm_key]


def warm_cache():
    """
    Pre-fetches and normalizes both boards, in parallel, so the cache is
    already populated before any real user asks a question. Called from
    main.py's FastAPI startup event in a background thread - doesn't block
    the server from becoming ready, but means the *first* real request
    doesn't pay the full cold-fetch latency (two sequential monday.com
    round trips, now down to one each after the query fix) on top of the
    LLM call.

    Safe to call multiple times or concurrently with a real request; the
    underlying _cache dict just gets populated once, idempotently (worst
    case under a race is one redundant fetch, not incorrect data).
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(_cached_normalized_board, DEALS_BOARD_ID, "deals", DEALS_DATE_FIELDS)
        pool.submit(_cached_normalized_board, WORK_ORDERS_BOARD_ID, "work_orders", WORK_ORDERS_DATE_FIELDS)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_deals_summary",
            "description": (
                "Get an aggregated summary of the Deals board (sales pipeline): "
                "deal counts and total value grouped by Deal Stage and by "
                "Sector/service, plus missing-data stats for key fields (Closure "
                "Probability, Masked Deal value, Close Date, Sector/service). Use "
                "this for any 'how's pipeline/revenue/sector looking' style "
                "question - it's compact and gives the real numbers directly, "
                "no need to also fetch raw rows unless the user wants a list of "
                "specific deals."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deals_rows",
            "description": (
                "Fetch individual deal rows from the Deals board, optionally "
                "filtered (case-insensitive substring match). Use this when the "
                "user wants specific deals listed (e.g. 'which deals have no "
                "close date', 'list open energy deals'), not for aggregate "
                "questions - use get_deals_summary for those. Results are capped "
                "(default 25) with a total match count returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string", "description": "Filter by Sector/service (substring match), e.g. 'energy'"},
                    "stage": {"type": "string", "description": "Filter by Deal Stage (substring match)"},
                    "status": {"type": "string", "description": "Filter by Deal Status (substring match)"},
                    "missing_field": {
                        "type": "string",
                        "description": "Instead of substring filters, return rows where THIS field is blank/missing. E.g. 'Close Date (A)' to find deals with no close date. Valid values: 'Close Date (A)', 'Closure Probability', 'Masked Deal value', 'Sector/service', 'Tentative Close Date'.",
                    },
                    "limit": {"type": "integer", "description": "Max rows to return, default 20 (hard cap 30)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_work_orders_summary",
            "description": (
                "Get an aggregated summary of the Work Orders board (project "
                "execution / billing): row counts grouped by Execution Status, "
                "Sector, and Billing Status, plus total Amount Receivable and "
                "missing-data stats for key fields. Use this for any "
                "operational-health or billing-health question."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_work_orders_rows",
            "description": (
                "Fetch individual work order rows, optionally filtered "
                "(case-insensitive substring match). Use this for specific "
                "row-level lookups, not aggregate questions - use "
                "get_work_orders_summary for those. Results are capped (default "
                "25) with a total match count returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string", "description": "Filter by Sector (substring match)"},
                    "execution_status": {"type": "string", "description": "Filter by Execution Status (substring match)"},
                    "billing_status": {"type": "string", "description": "Filter by Billing Status (substring match)"},
                    "missing_field": {
                        "type": "string",
                        "description": "Instead of substring filters, return rows where THIS field is blank/missing. Valid values: 'Sector', 'Billing Status', 'Amount Receivable (Masked)', 'Collection Date'.",
                    },
                    "limit": {"type": "integer", "description": "Max rows to return, default 20 (hard cap 30)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deal_execution_status",
            "description": (
                "Look up ONE specific deal by name and see its status on BOTH "
                "boards side by side: its sales-pipeline info from Deals, and "
                "its execution/billing info from Work Orders, joined by deal "
                "name. Use this for questions about a specific named deal's "
                "end-to-end status (e.g. 'is Sakura's deal in production yet', "
                "'what's the billing status on the Naruto deal')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deal_name": {"type": "string", "description": "Deal name to look up (substring match, case-insensitive)"},
                },
                "required": ["deal_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deals_missing_work_orders",
            "description": (
                "Cross-board check: find deals that exist on the Deals board "
                "but have NO matching row on the Work Orders board (joined by "
                "deal name), optionally filtered by Deal Stage. Use this for "
                "questions like 'which won deals don't have a work order yet' "
                "- this computes the join directly rather than you having to "
                "compare two separate row dumps yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "description": "Filter to deals whose Deal Stage contains this substring, e.g. 'Won'. Omit to check all deals regardless of stage."},
                    "limit": {"type": "integer", "description": "Max rows to return, default 25 (hard cap 30)"},
                },
            },
        },
    },
]

SYSTEM_PROMPT = f"""You are Skylark Drones' internal Business Intelligence agent. \
Founders and execs ask you plain-language questions and you answer using live \
data pulled from two monday.com boards:

- Deals board: sales pipeline (deal stage, value, sector, close dates, owner).
- Work Orders board: project execution and billing (status, invoicing, collection).

Today's date is {date.today().isoformat()}.

Rules you always follow:
1. Never invent or assume numbers. Always call a tool to get current data before \
answering anything factual about deals or work orders - even if you think you \
already fetched it earlier in the conversation, prefer fetching again if the \
user's question is about current/live state.
2. Prefer the summary tools (get_deals_summary, get_work_orders_summary) for any \
aggregate/health/trend question - they're pre-computed and compact. Use the \
row-level tools (get_deals_rows, get_work_orders_rows) when the user wants a list \
of specific individual deals/orders, and note if results were truncated. Use \
get_deal_execution_status for one named deal's status across both boards, and \
get_deals_missing_work_orders for "which deals lack a work order" style questions \
- these compute the cross-board join directly rather than you comparing two row \
dumps by eye.
3. This is real, messy data. Expect missing values, inconsistent casing, blank \
dates, masked codes instead of real names. Handle this gracefully:
   - Don't crash or refuse because of nulls - work with what's there.
   - The summary tools return explicit missing-data percentages - always mention \
these as caveats when they're relevant to the question, not as a footnote buried \
at the end.
   - Date fields have already been normalized server-side into a consistent \
YYYY-MM-DD form regardless of how they were originally entered (slashes, month \
names, etc.) - a small number may still be unparseable garbage values, which the \
data itself flags rather than silently dropping.
   - Normalize inconsistent text yourself when reasoning about it, but don't \
silently paper over data quality issues - surface them.
4. If a question is ambiguous (e.g. "this quarter" - calendar or fiscal? which \
sector taxonomy?), ask a brief clarifying question rather than guessing silently, \
unless a reasonable default is obvious - then state the default you're using.
5. Give insight, not just numbers. A founder wants to know what a number means: \
is this good/bad, what's driving it, what's the risk. Keep it tight - a few \
sentences or a short list, not an essay - founders are busy.
6. You can query across both boards when a question needs it (e.g. linking a \
deal to its work order via the deal name) - use the dedicated cross-board tools \
for this rather than guessing at matches yourself.
7. If asked to "prepare a leadership update" or similar, produce a concise \
written brief: pipeline health, operational/billing status, and a short list of \
data-quality caveats - suitable for pasting into a leadership doc or email.
8. Formatting: never use a "$" character (it breaks this chat UI's rendering) - \
write "Rs." or just the number with "M"/"B"/"Cr" suffixes instead. Never use raw \
HTML tags like <br> - use separate bullet points or markdown table rows instead. \
Keep tables simple: one fact per cell, no packed multi-line cells.
"""


DEALS_COMPACT_FIELDS = ["Deal Stage", "Sector/service", "Deal Status", "Close Date (A)", "Masked Deal value", "Closure Probability"]
WORK_ORDERS_COMPACT_FIELDS = ["Execution Status", "Sector", "Billing Status", "Amount Receivable (Masked)"]


def _call_tool(name: str, args: dict) -> str:
    try:
        if name == "get_deals_summary":
            rows = _cached_normalized_board(DEALS_BOARD_ID, "deals", DEALS_DATE_FIELDS)
            summary = summarize_rows(
                rows,
                group_by=["Deal Stage"],
                value_field="Masked Deal value",
                missing_check_fields=["Closure Probability", "Masked Deal value", "Close Date (A)", "Sector/service"],
            )
            by_sector = summarize_rows(rows, group_by=["Sector/service"], value_field="Masked Deal value")
            summary["by_sector"] = by_sector["breakdown"]
            summary["date_normalization"] = _date_norm_stats.get("deals", {})
            return json.dumps(summary)

        if name == "get_deals_rows":
            rows = _cached_normalized_board(DEALS_BOARD_ID, "deals", DEALS_DATE_FIELDS)
            filters = {
                "Sector/service": args.get("sector"),
                "Deal Stage": args.get("stage"),
                "Deal Status": args.get("status"),
            }
            result = filter_rows(
                rows, filters, limit=args.get("limit", 20),
                missing_field=args.get("missing_field"),
                fields=DEALS_COMPACT_FIELDS,
            )
            return json.dumps(result)

        if name == "get_work_orders_summary":
            rows = _cached_normalized_board(WORK_ORDERS_BOARD_ID, "work_orders", WORK_ORDERS_DATE_FIELDS)
            summary = summarize_rows(
                rows,
                group_by=["Execution Status"],
                missing_check_fields=["Sector", "Billing Status", "Amount Receivable (Masked)"],
            )
            by_sector = summarize_rows(rows, group_by=["Sector"])
            by_billing = summarize_rows(
                rows, group_by=["Billing Status"], value_field="Amount Receivable (Masked)"
            )
            summary["by_sector"] = by_sector["breakdown"]
            summary["by_billing_status"] = by_billing["breakdown"]
            summary["date_normalization"] = _date_norm_stats.get("work_orders", {})
            return json.dumps(summary)

        if name == "get_work_orders_rows":
            rows = _cached_normalized_board(WORK_ORDERS_BOARD_ID, "work_orders", WORK_ORDERS_DATE_FIELDS)
            filters = {
                "Sector": args.get("sector"),
                "Execution Status": args.get("execution_status"),
                "Billing Status": args.get("billing_status"),
            }
            result = filter_rows(
                rows, filters, limit=args.get("limit", 20),
                missing_field=args.get("missing_field"),
                fields=WORK_ORDERS_COMPACT_FIELDS,
            )
            return json.dumps(result)

        if name == "get_deal_execution_status":
            deal_name = str(args.get("deal_name", "")).strip()
            deals = _cached_normalized_board(DEALS_BOARD_ID, "deals", DEALS_DATE_FIELDS)
            wos = _cached_normalized_board(WORK_ORDERS_BOARD_ID, "work_orders", WORK_ORDERS_DATE_FIELDS)
            needle = deal_name.lower()
            deal_matches = filter_rows(deals, {"name": needle}, limit=10, fields=DEALS_COMPACT_FIELDS)
            wo_matches = filter_rows(wos, {"name": needle}, limit=10, fields=WORK_ORDERS_COMPACT_FIELDS)
            return json.dumps({
                "deal_side": deal_matches,
                "work_order_side": wo_matches,
                "note": "Joined by deal name (substring match). A deal with no work_order_side "
                        "matches has not reached execution yet, or the naming doesn't line up.",
            })

        if name == "get_deals_missing_work_orders":
            deals = _cached_normalized_board(DEALS_BOARD_ID, "deals", DEALS_DATE_FIELDS)
            wos = _cached_normalized_board(WORK_ORDERS_BOARD_ID, "work_orders", WORK_ORDERS_DATE_FIELDS)
            result = find_unmatched(
                deals, wos,
                filter_field="Deal Stage", filter_value=args.get("stage"),
                limit=args.get("limit", 25),
            )
            return json.dumps(result)

        return json.dumps({"error": f"Unknown tool {name}"})
    except MondayClientError as e:
        return json.dumps({
            "error": "monday.com API error",
            "detail": str(e),
            "note": "Tell the user this board couldn't be reached and they "
                    "should check MONDAY_API_TOKEN / board ID configuration.",
        })


def run_agent(conversation: list[dict], client: OpenAI) -> tuple[str, list[dict], list[str]]:
    """
    conversation: list of {"role": "user"|"assistant", "content": str} - plain
    chat turns only. Internally we prepend the system prompt and manage the
    tool-call round trips ourselves; the returned conversation stays in this
    same plain user/assistant shape so it's safe to persist in session state.
    Returns (final_text_reply, updated_conversation, tools_called).

    tools_called is returned so the UI can show a transparency trace of what
    was actually queried live from monday.com for this answer - makes the
    "not hardcoded, really calling the API" claim visible, not just asserted.

    Only the last MAX_HISTORY_TURNS exchanges are sent to the model - free-tier
    token limits (as low as 8,000 tokens/minute) mean an unbounded, growing
    conversation history will eventually blow the budget on a long chat even
    if each individual turn is small. Full history is still kept in session
    state / returned here for display - only the model-facing prompt is
    trimmed.
    """
    MAX_HISTORY_TURNS = 4  # ~8 messages (user+assistant pairs)
    trimmed = conversation[-(MAX_HISTORY_TURNS * 2):]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + list(trimmed)
    tools_called: list[str] = []

    for _ in range(6):  # safety cap on tool-use round trips
        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=1000,  # trimmed from 1500 - system prompt already asks for tight
                              # answers; a smaller cap means faster generation on Groq
            tools=TOOLS,
            messages=messages,
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            final_text = msg.content or ""
            updated = list(conversation) + [{"role": "assistant", "content": final_text}]
            return final_text, updated, tools_called

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            tools_called.append(tc.function.name)
            result = _call_tool(tc.function.name, args)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    fallback = "I ran into trouble completing that request (too many data lookups). Try narrowing the question."
    return fallback, list(conversation) + [{"role": "assistant", "content": fallback}], tools_called


def get_kpis() -> dict:
    """
    A handful of top-line numbers for the dashboard header, computed the same
    way the summary tools do. Cheap (uses the same in-memory board cache) and
    meant to give an at-a-glance pulse before anyone types a question.
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        deals_future = pool.submit(_cached_board, DEALS_BOARD_ID, "deals")
        wo_future = pool.submit(_cached_board, WORK_ORDERS_BOARD_ID, "work_orders")
        deals = deals_future.result()
        work_orders = wo_future.result()

    deal_summary = summarize_rows(
        deals, group_by=["Deal Stage"], value_field="Masked Deal value",
        missing_check_fields=["Closure Probability", "Masked Deal value", "Close Date (A)", "Sector/service"],
    )
    won = sum(g["count"] for g in deal_summary["breakdown"] if str(g.get("Deal Stage", "")).startswith("G."))
    total_known_value = sum(g.get("Masked Deal value_sum", 0) for g in deal_summary["breakdown"])

    wo_summary = summarize_rows(
        work_orders, group_by=["Execution Status"],
        missing_check_fields=["Sector", "Billing Status", "Amount Receivable (Masked)"],
    )
    ongoing = sum(g["count"] for g in wo_summary["breakdown"]
                  if "ongoing" in str(g.get("Execution Status", "")).lower()
                  or "executed" in str(g.get("Execution Status", "")).lower())

    # Rough data-completeness score: 100 minus the average missing-% across
    # the fields we track on both boards.
    all_missing_pcts = (
        [v["missing_pct"] for v in deal_summary["missing_data"].values()] +
        [v["missing_pct"] for v in wo_summary["missing_data"].values()]
    )
    completeness = round(100 - (sum(all_missing_pcts) / len(all_missing_pcts)), 1) if all_missing_pcts else None

    return {
        "total_deals": deal_summary["total_rows"],
        "won_deals": won,
        "total_known_pipeline_value": round(total_known_value, 2),
        "total_work_orders": wo_summary["total_rows"],
        "ongoing_work_orders": ongoing,
        "data_completeness_pct": completeness,
    }


def get_dashboard_charts() -> dict:
    """
    Chart-ready aggregates for the dashboard tab: deal counts by stage, deal
    value by sector, work order counts by execution status, and by billing
    status. Each is a list of {label, value} - plain enough to hand straight
    to st.bar_chart without any extra charting library.
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        deals_future = pool.submit(_cached_board, DEALS_BOARD_ID, "deals")
        wo_future = pool.submit(_cached_board, WORK_ORDERS_BOARD_ID, "work_orders")
        deals = deals_future.result()
        work_orders = wo_future.result()

    by_stage = summarize_rows(deals, group_by=["Deal Stage"])
    by_sector_value = summarize_rows(deals, group_by=["Sector/service"], value_field="Masked Deal value")
    by_exec_status = summarize_rows(work_orders, group_by=["Execution Status"])
    by_billing_status = summarize_rows(work_orders, group_by=["Billing Status"])

    return {
        "deals_by_stage": [{"label": g["Deal Stage"], "value": g["count"]} for g in by_stage["breakdown"]],
        "deal_value_by_sector": [
            {"label": g["Sector/service"], "value": g.get("Masked Deal value_sum", 0)}
            for g in by_sector_value["breakdown"]
        ],
        "work_orders_by_status": [{"label": g["Execution Status"], "value": g["count"]} for g in by_exec_status["breakdown"]],
        "work_orders_by_billing": [{"label": g["Billing Status"], "value": g["count"]} for g in by_billing_status["breakdown"]],
    }
