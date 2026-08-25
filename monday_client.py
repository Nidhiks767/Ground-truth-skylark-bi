"""
Thin client for monday.com's GraphQL API (v2).

Only does reads - fetching board columns and items - matching the
assignment's "read only" integration requirement. No mutations.
"""
import os
import requests

MONDAY_API_URL = "https://api.monday.com/v2"


class MondayClientError(Exception):
    pass


def _headers():
    token = os.environ.get("MONDAY_API_TOKEN")
    if not token:
        raise MondayClientError("MONDAY_API_TOKEN environment variable is not set")
    return {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }


def _run_query(query: str, variables: dict | None = None) -> dict:
    resp = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables or {}},
        headers=_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise MondayClientError(str(data["errors"]))
    return data["data"]


def get_board_columns(board_id: str) -> list[dict]:
    """Returns [{id, title, type}] for a board. Kept as a standalone helper
    (used elsewhere / useful for debugging), but get_board_items below no
    longer calls this separately - it fetches columns and items together in
    one request to cut a network round trip."""
    query = """
    query ($boardId: [ID!]) {
      boards(ids: $boardId) {
        columns { id title type }
      }
    }
    """
    data = _run_query(query, {"boardId": [board_id]})
    boards = data.get("boards") or []
    if not boards:
        raise MondayClientError(f"Board {board_id} not found or not accessible with this token")
    return boards[0]["columns"]


def get_board_items(board_id: str, limit: int = 500, max_pages: int = 20) -> list[dict]:
    """
    Returns every item on a board as a list of dicts:
    { "name": <item name>, "<Column Title>": <text value>, ... }

    Fetches columns and the first page of items in a single GraphQL request
    (rather than two separate round trips) since both are available on the
    same `boards` query. Paginates via items_page cursor until exhausted or
    max_pages hit (safety valve - our boards are ~350 and ~180 rows, well
    under one page, so in practice this is exactly one network call).
    """
    query = """
    query ($boardId: [ID!], $limit: Int!, $cursor: String) {
      boards(ids: $boardId) {
        columns { id title type }
        items_page(limit: $limit, cursor: $cursor) {
          cursor
          items {
            name
            column_values { id text }
          }
        }
      }
    }
    """

    items_out = []
    cursor = None
    col_title_by_id = None
    for _ in range(max_pages):
        data = _run_query(query, {"boardId": [board_id], "limit": limit, "cursor": cursor})
        boards = data.get("boards") or []
        if not boards:
            if col_title_by_id is None:
                raise MondayClientError(f"Board {board_id} not found or not accessible with this token")
            break
        board = boards[0]
        if col_title_by_id is None:
            col_title_by_id = {c["id"]: c["title"] for c in board["columns"]}
        page = board["items_page"]
        for item in page["items"]:
            row = {"name": item["name"]}
            for cv in item["column_values"]:
                title = col_title_by_id.get(cv["id"], cv["id"])
                row[title] = cv["text"]  # None/"" if empty - left as-is, agent handles nulls
            items_out.append(row)
        cursor = page.get("cursor")
        if not cursor:
            break
    return items_out
