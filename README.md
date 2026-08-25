# Ground Truth — Skylark Drones BI Agent

A conversational agent that answers founder-level business questions by
reading live data from two monday.com boards (Deals, Work Orders).

Named for the aerial-survey term: "ground truth" is the verified real-world
data used to calibrate what's measured from the air - a fitting name for a
tool whose entire job is giving founders the actual verified numbers instead
of guesses, for a company whose core business is aerial survey.

Primary version: **FastAPI backend + vanilla JS/HTML frontend** (`main.py` +
`static/`). A Streamlit version (`streamlit_app.py`) is also included as a
lighter-weight alternative front end over the exact same backend logic.

## Architecture

```
Browser (static/index.html, app.js)
    |  fetch() -> REST API
FastAPI app  (main.py)
    |  /api/kpis, /api/dashboard, /api/chat
LLM tool-use loop  (agent.py)  -- Groq API (OpenAI-compatible), free tier
    |
  tools: get_deals_summary / get_deals_rows /
         get_work_orders_summary / get_work_orders_rows /
         get_deal_execution_status / get_deals_missing_work_orders
    |
data_shaping.py  -- aggregation & filtering (keeps payloads small)
    |
monday.com GraphQL client  (monday_client.py)
    |
monday.com API (read-only)
```

The reasoning/data layer (`agent.py`, `monday_client.py`, `data_shaping.py`)
is completely UI-agnostic - `main.py` and `streamlit_app.py` are two
interchangeable front ends over the exact same backend logic, proof the
business logic isn't tied to any one presentation layer.

- **main.py** - FastAPI backend. Three endpoints: `GET /api/kpis` (headline
  numbers), `GET /api/dashboard` (chart-ready aggregates), `POST /api/chat`
  (the agent loop). Also serves `static/` as the frontend, so the whole app
  is one deployable service. Typed request/response models via Pydantic;
  errors surface as proper HTTP status codes (500 for missing config, 502
  for upstream monday.com/Groq failures) rather than crashing.
- **static/** - the frontend: `index.html` (structure), `style.css` (a
  "ground control / telemetry" visual theme grounded in Skylark's actual
  industry - aerial inspection - rather than a generic chat-app look),
  `app.js` (fetch calls, chat state, Chart.js rendering, markdown rendering
  via marked.js + DOMPurify for XSS safety on LLM-generated HTML). No build
  step - plain JS, loaded via CDN script tags.
- **agent.py** - the system prompt, tool definitions, and the tool-use loop:
  the model decides when to call a tool, we run it against monday.com, feed
  the result back, and let it keep going until it has a final answer. Uses
  Groq's OpenAI-compatible API (function calling) rather than a
  provider-specific SDK, and returns which tools were called so the UI can
  show a live "queried: ..." trace on every answer.
- **data_shaping.py** - turns raw monday.com rows into small, LLM-friendly
  shapes: pre-aggregated summaries (counts/sums grouped by stage, sector,
  status, plus missing-data percentages), filtered/capped row lookups, an
  explicit date normalizer (`normalize_date`/`normalize_dates_in_rows` -
  parses messy date formats like `26/12/2025`, `Dec 26, 2025` into a single
  canonical `YYYY-MM-DD` form, flagging genuinely unparseable values rather
  than silently dropping them), and a cross-board join helper
  (`find_unmatched` - e.g. "which won deals have no matching work order").
- **monday_client.py** - a minimal read-only GraphQL client. Fetches board
  columns once, then paginates `items_page` to pull every row, returning
  each row as `{column title: text value}`. No data is cached to disk or
  hardcoded - every session re-fetches from monday.com (an in-memory cache
  avoids re-fetching on every single chat turn, cleared on restart).
- **streamlit_app.py** - alternate front end, same backend logic, useful for
  a quick local demo without the FastAPI/static setup.

## Why this stack

- **FastAPI + vanilla JS over Streamlit-only**: a real REST API with typed
  models and HTTP-status error handling, decoupled from the presentation
  layer, is closer to how this would actually be built in production and
  demonstrates the backend/frontend split explicitly rather than leaning on
  a data-app framework that blurs the two together. Trade-off: more surface
  area to maintain (two files' worth of frontend vs. one Streamlit script) -
  `streamlit_app.py` is kept as a faster-to-read fallback.
- **No frontend build step**: plain HTML/CSS/JS with CDN-loaded libraries
  (Chart.js, marked.js, DOMPurify) instead of React/Vite. This keeps
  deployment to a single Python service with zero Node tooling in the
  pipeline - one less thing that can break between "works locally" and
  "works on the hosted link," which matters more here than framework
  polish.
- **monday.com REST/GraphQL API over MCP**: simpler to reason about and debug
  for this timeline; a GraphQL client is ~80 lines and fully under our
  control. monday.com's official MCP server is a reasonable alternative if
  the goal is broader/longer-term reuse.
- **Tool-use loop over a fixed pipeline**: the two boards are small enough
  (≈350 and ≈180 rows) to fetch in full and let the model reason over
  pre-aggregated data, rather than building a rigid query/filter DSL. This
  also lets the agent naturally handle messy data (nulls, inconsistent
  casing) the way a person would, instead of a brittle parser choking on it.
- **Groq (free tier) over a paid frontier model API**: Groq's free tier
  requires no credit card and supports the same function/tool-calling loop
  this agent needs, at no cost. Trade-off: the open-weight model's reasoning
  and instruction-following is a step below top-tier proprietary models,
  which can occasionally show up in edge-case query understanding or minor
  arithmetic slips in written summaries. The agent code is provider-agnostic
  at the interface level (standard function-calling, OpenAI-compatible
  client) - swapping in Claude, GPT, or Gemini later is a small, contained
  change to `agent.py`, not an architecture change.

## Setup

1. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

2. **Get a free Groq API key**
   - Sign up at [console.groq.com](https://console.groq.com) (no credit card
     required for the free tier).
   - Create an API key under API Keys.

3. **Configure monday.com**
   - Two boards must exist: a Deals board and a Work Orders board, imported
     from the provided files.
   - Generate a Personal API token: monday.com → profile icon → Admin → API.
   - Note both board IDs (visible in each board's URL:
     `.../boards/<BOARD_ID>`).

4. **Configure secrets**
   Copy `.env.example` to `.env` and fill in:
   ```
   GROQ_API_KEY=...
   MONDAY_API_TOKEN=...
   MONDAY_DEALS_BOARD_ID=...
   MONDAY_WORK_ORDERS_BOARD_ID=...
   ```
   On your hosting platform (Render, Railway, etc.), set these as
   environment variables in the service dashboard instead of a `.env` file.

5. **Run locally**
   ```
   uvicorn main:app --reload
   ```
   Then open http://localhost:8000

   (Or, for the Streamlit alternative: `streamlit run streamlit_app.py`)

6. **Deploy**
   Push this folder to a GitHub repo. On [Render](https://render.com) (or
   Railway): New → Web Service → connect the repo → set the four
   environment variables from step 4 → the `Procfile` in this repo already
   defines the start command (`uvicorn main:app --host 0.0.0.0 --port
   $PORT`), so no build/start command config is usually needed beyond
   pointing Render at this repo. Deploy - you'll get a public
   `*.onrender.com` link, testable in any browser with no local setup.

## Verifying the monday.com connection

If the app reports a monday.com error, the fastest check is a raw curl:
```bash
curl -s https://api.monday.com/v2 \
  -H "Authorization: $MONDAY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ boards(ids: [YOUR_BOARD_ID]) { name } }"}'
```
A successful response returns the board name; an auth error returns an
`errors` array (usually an expired/incorrect token). You can also hit the
backend's own health/data endpoints directly once deployed:
`GET /api/health`, `GET /api/kpis`, `GET /api/dashboard`.

## Known limitations

- Full-board-per-query works well at this data size (a few hundred rows per
  board) but wouldn't scale to boards with tens of thousands of items -
  would need server-side filtering via monday's `query_params` at that
  point.
- No conversation persistence across sessions/browser refreshes (in-memory
  only, on both the FastAPI and Streamlit versions).
- Read-only by design, per the assignment's integration requirements.
- The free-tier open-weight model occasionally slips on arithmetic embedded
  in prose (e.g. stating a percentage inconsistently within the same
  answer) even when the underlying tool data is correct - a known trade-off
  of the free/open-weight model choice, documented in the Decision Log.
