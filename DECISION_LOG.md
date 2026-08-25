# Decision Log

## Key assumptions

- **Header row quirk**: the Work Order Tracker file has a blank first row
  with real headers on row 2. Imported directly into monday.com by selecting
  row 2 as the header row during import - no data was edited, only the
  import's header-row pointer.
- **Column types**: date-like fields were set to Date columns, monetary
  fields to Numbers, and status-like fields (Deal Status, Execution Status,
  Billing Status, etc.) to Status/label columns on import. Everything else
  left as Text, since the agent works from column text values regardless of
  underlying type.
- **"This quarter" and similar relative time references**: interpreted as
  the current calendar quarter unless the user specifies otherwise. The
  agent is instructed to state this assumption explicitly rather than
  silently guess, and to ask if genuinely ambiguous.
- **Masked/coded fields** (Owner code, Client Code, WOCOMPANY_xxx): treated
  as opaque identifiers, not resolved to real names - the agent reports on
  them as-is, since no mapping table was provided.
- **Data quality is a first-class output, not an afterthought**: given the
  brief explicitly calls out messy real-world data, the agent is instructed
  to surface caveats (e.g. "40% of deals have no Closure Probability set")
  inline with its answer rather than burying them in a footnote or ignoring
  them.

## Trade-offs chosen and why

- **Fetch full board vs. server-side filtered queries**: at ~350 and ~180
  rows, fetching each board in full and letting Claude reason over raw JSON
  is simpler, more robust to messy/inconsistent data, and avoids building a
  brittle query-translation layer. Trade-off: wouldn't scale to very large
  boards without adding real filtering via monday's API query params.
- **In-memory cache, not a database**: a session-scoped cache avoids
  re-fetching monday.com on every chat turn without adding infrastructure.
  Trade-off: data can go stale within a long session; acceptable for a BI
  assistant answering questions minutes apart, not for real-time dashboards.
- **Direct GraphQL client over monday's official MCP server**: gave full
  control and transparency for a 6-hour build and is easy for a grader to
  read end-to-end in ~80 lines. Trade-off: an MCP server would be less code
  to maintain long-term and pairs more naturally with other MCP-based tools.
- **Pre-aggregation over raw-dump tools**: initially the agent fetched and
  sent every raw row to the model on every question. This broke almost
  immediately against free-tier LLM rate limits (Groq's free tier caps at
  6,000-8,000 tokens/minute; the two boards combined as raw JSON run to
  ~35,000+ tokens). The fix - `data_shaping.py` - is also just better BI
  design: pre-computing counts/sums grouped by stage/sector/status, plus
  explicit missing-data percentages, gives the model (and by extension the
  founder) the rollup directly instead of making an LLM eyeball 350 raw
  rows to compute a sum. Row-level detail is still available on demand via
  filtered, capped lookup tools for "which specific deals..." questions.
- **Explicit date normalization pass over relying on monday.com's own
  formatting**: monday.com Date-type columns are already consistent, but a
  field imported as plain Text (or containing operator-entered free text)
  is not guaranteed to be. Added a dedicated normalizer
  (`normalize_date`/`normalize_dates_in_rows`) that parses common messy
  formats (`26/12/2025`, `Dec 26, 2025`, ISO variants) into one canonical
  `YYYY-MM-DD` form before any date field is grouped or filtered, and
  reports how many values were actually reformatted vs. left untouched vs.
  genuinely unparseable per field - so the normalization is auditable, not
  a silent transformation. Applied dd/mm/yyyy-first parsing since this is
  Indian business data.
- **Dedicated cross-board join tools over relying on the model to
  self-join**: the agent could always call both boards' tools in one turn
  and reason across them, but that leaves the actual name-matching to the
  LLM eyeballing two separate row dumps - unreliable at row-count scale.
  Added `get_deal_execution_status` (one named deal, both boards side by
  side) and `get_deals_missing_work_orders` (server-side join computing
  which deals - optionally filtered by stage - have no matching Work Order
  row by name) so this is a real, tested capability rather than an
  emergent one.
- **Streamlit as an alternate, not primary, front end**: fastest path to a
  first working link, kept as `streamlit_app.py` for quick local demos.
  Trade-off: Streamlit blurs backend and frontend into one script - the
  primary deliverable (`main.py` + `static/`) was rebuilt as a proper FastAPI
  REST API with a hand-written JS frontend specifically to demonstrate that
  split explicitly, since `agent.py`/`monday_client.py`/`data_shaping.py`
  were already UI-agnostic. This made the swap a presentation-layer change,
  not a rewrite of the underlying logic. Trade-off: more files to maintain,
  and a hand-rolled frontend takes more manual DOM/state work than a
  component framework would - acceptable at this scale.

## What I'd do differently with more time

- Add real server-side filtering/pagination for scale, and incremental
  sync instead of full-board refetch.
- Build a small evaluation set of founder-style questions with expected
  answer shapes, to catch regressions in agent behavior systematically
  rather than spot-checking.
- Add a lightweight mapping layer for masked codes (owner/client/company) if
  a real mapping table exists internally, so answers can use real names.
- Persist conversation history (e.g. to a small database) so sessions
  survive a page refresh.
- Add charts/visual summaries (e.g. pipeline by stage, sector breakdown) 
  alongside text answers for the leadership-update feature.

## Interpretation of "help prepare data for leadership updates"

Implemented as an explicit quick-action in the chat UI ("Prepare leadership
update") that triggers a structured prompt asking the agent to pull both
boards and produce a short brief covering: pipeline health by stage and
sector, operational/billing status from work orders, and a concise list of
data-quality caveats - written so it can be pasted directly into an email or
leadership doc. This was chosen over an automated recurring report (e.g. a
scheduled email) since the assignment's scope is a conversational agent, and
an on-demand, chat-triggered brief fits that interaction model while still
directly addressing the "prepare data for leadership" requirement.

## Performance: cold-start latency

Early testing showed a slow first response (the KPI strip and first chat
answer both took a noticeably long time to load). Root cause was two-fold:
`monday_client.get_board_items` made two sequential network round trips per
board (a columns query, then an items query) when both are available on the
same GraphQL query; and the very first real question always paid the full
cost of an uncached board fetch on top of the LLM call. Fixed both:
`get_board_items` now fetches columns and items in a single request, and
`main.py`'s FastAPI startup event pre-fetches and normalizes both boards in
a background thread the moment the server boots (parallelized across the
two boards), so the cache is typically already warm by the time a real user
asks a question. The warm-up thread is deliberately non-blocking and fails
silently - it never delays the server becoming ready, and if monday.com is
genuinely unreachable, the normal per-request error handling still surfaces
that clearly rather than the background thread masking it.
