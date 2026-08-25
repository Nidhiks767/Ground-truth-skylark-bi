"""
FastAPI backend for Ground Truth, Skylark Drones' BI agent.

This is the "full-stack" version of the project: a proper REST API layer
(this file) in front of the same agent/monday.com logic used everywhere else
in this project, with a separate vanilla JS/HTML frontend (static/) consuming
it over fetch(). The reasoning layer (agent.py, monday_client.py,
data_shaping.py) is completely UI-agnostic - this file and streamlit_app.py
are two interchangeable front ends over the exact same backend logic.

Run locally:  uvicorn main:app --reload
Deploy:       uvicorn main:app --host 0.0.0.0 --port $PORT   (see Procfile)
"""
import os
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent import run_agent, get_client, get_kpis, get_dashboard_charts, warm_cache, DEALS_BOARD_ID, WORK_ORDERS_BOARD_ID

app = FastAPI(title="Ground Truth — Skylark BI Agent API", version="1.0")

# Open CORS since the frontend may be served from a different origin during
# local dev (e.g. a separate `python -m http.server` for static/). In the
# single-service deployment (this app also serves static/), origin is the
# same and this has no practical effect.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_client = None


def _shared_client():
    """Reuse one Groq/OpenAI-compatible client across requests instead of
    constructing a new one per call."""
    global _client
    if _client is None:
        _client = get_client()
    return _client


def _check_config():
    missing = [n for n, v in [
        ("GROQ_API_KEY", os.environ.get("GROQ_API_KEY")),
        ("MONDAY_API_TOKEN", os.environ.get("MONDAY_API_TOKEN")),
        ("MONDAY_DEALS_BOARD_ID", DEALS_BOARD_ID),
        ("MONDAY_WORK_ORDERS_BOARD_ID", WORK_ORDERS_BOARD_ID),
    ] if not v]
    if missing:
        raise HTTPException(status_code=500, detail=f"Missing environment variables: {', '.join(missing)}")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    conversation: list[ChatMessage]


class ChatResponse(BaseModel):
    reply: str
    tools_called: list[str]
    conversation: list[ChatMessage]


@app.on_event("startup")
def _warm_cache_on_startup():
    """
    Kick off a background fetch of both monday.com boards the moment the
    server starts, rather than waiting for the first real user's request to
    trigger it. Runs in a daemon thread so it never blocks the server from
    becoming ready (e.g. for the hosting platform's health check) - if
    misconfigured env vars make this fail, it fails silently here and the
    normal per-request error handling in /api/kpis, /api/chat etc. still
    surfaces the real error to the user as usual.
    """
    import threading

    def _safe_warm():
        try:
            warm_cache()
        except Exception:
            pass  # real errors still surface per-request via the normal endpoints

    threading.Thread(target=_safe_warm, daemon=True).start()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/kpis")
def kpis():
    _check_config()
    try:
        return get_kpis()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"monday.com error: {e}")


@app.get("/api/dashboard")
def dashboard():
    _check_config()
    try:
        return get_dashboard_charts()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"monday.com error: {e}")


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    _check_config()
    client = _shared_client()
    conversation = [m.model_dump() for m in req.conversation]
    try:
        reply, updated_conversation, tools_called = run_agent(conversation, client)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Agent error: {e}")
    return ChatResponse(reply=reply, tools_called=tools_called, conversation=updated_conversation)


# Serve the frontend. Mounted last so the /api/* routes above always take
# priority over the static file catch-all.
app.mount("/", StaticFiles(directory="static", html=True), name="static")
