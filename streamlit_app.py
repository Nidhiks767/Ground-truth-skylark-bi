import os
from dotenv import load_dotenv
load_dotenv()  # no-op if no .env file present (e.g. on Streamlit Cloud, where
                # secrets come from the platform's secrets manager instead)

import streamlit as st
import pandas as pd

from agent import run_agent, get_client, get_kpis, get_dashboard_charts, DEALS_BOARD_ID, WORK_ORDERS_BOARD_ID

TOOL_LABELS = {
    "get_deals_summary": "📈 Deals board (summary)",
    "get_deals_rows": "📋 Deals board (row lookup)",
    "get_work_orders_summary": "📈 Work Orders board (summary)",
    "get_work_orders_rows": "📋 Work Orders board (row lookup)",
    "get_deal_execution_status": "🔗 Cross-board lookup (one deal)",
    "get_deals_missing_work_orders": "🔗 Cross-board join (won deals without a work order)",
}


def _sanitize(text: str) -> str:
    """Defensive cleanup regardless of whether the model followed the
    formatting rules in its system prompt: escape '$' so Streamlit doesn't
    try to render it as LaTeX math, and turn stray '<br>' tags into real
    line breaks instead of showing them as literal text."""
    text = text.replace("$", "\\$")
    text = text.replace("<br>", "  \n").replace("<br/>", "  \n").replace("<br />", "  \n")
    return text


st.set_page_config(page_title="Ground Truth — Skylark BI", page_icon="🛰️", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; max-width: 1100px; }
    .skylark-header {
        display: flex; align-items: center; gap: 14px; margin-bottom: 4px;
    }
    .skylark-badge {
        background: linear-gradient(135deg, #ff7a18, #af2896 70%);
        color: white; font-size: 0.7rem; font-weight: 600;
        padding: 3px 10px; border-radius: 999px; letter-spacing: 0.03em;
    }
    .kpi-card {
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px; padding: 14px 16px;
    }
    .kpi-label { font-size: 0.75rem; opacity: 0.65; text-transform: uppercase; letter-spacing: 0.04em; }
    .kpi-value { font-size: 1.5rem; font-weight: 700; margin-top: 2px; }
    .tool-trace {
        font-size: 0.75rem; opacity: 0.6; margin-top: 6px;
        border-top: 1px solid rgba(255,255,255,0.08); padding-top: 6px;
    }
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<div class="skylark-header"><h1 style="margin:0;">🛰️ Ground Truth</h1>'
    '<span class="skylark-badge">LIVE · monday.com</span></div>',
    unsafe_allow_html=True,
)
st.caption(
    "Founder-level answers on pipeline, deals, work orders, and billing — queried "
    "live from your monday.com boards on every question, never cached to disk."
)

missing = [
    name for name, val in [
        ("GROQ_API_KEY", os.environ.get("GROQ_API_KEY")),
        ("MONDAY_API_TOKEN", os.environ.get("MONDAY_API_TOKEN")),
        ("MONDAY_DEALS_BOARD_ID", DEALS_BOARD_ID),
        ("MONDAY_WORK_ORDERS_BOARD_ID", WORK_ORDERS_BOARD_ID),
    ] if not val
]
if missing:
    st.error(
        "Missing environment variables: " + ", ".join(missing) +
        ". Set these before the app can run (see README)."
    )
    st.stop()

client = get_client()

if "messages" not in st.session_state:
    st.session_state.messages = []  # what we show the user: [{role, content, tools?}]
if "raw_conversation" not in st.session_state:
    st.session_state.raw_conversation = []  # plain history sent back into run_agent
if "kpis" not in st.session_state:
    try:
        st.session_state.kpis = get_kpis()
        st.session_state.kpi_error = None
    except Exception as e:
        st.session_state.kpis = None
        st.session_state.kpi_error = str(e)

# ---- KPI strip ----
if st.session_state.kpis:
    k = st.session_state.kpis
    cols = st.columns(5)
    cards = [
        ("Active Deals", f"{k['total_deals']}"),
        ("Deals Won", f"{k['won_deals']}"),
        ("Known Pipeline Value", f"Rs. {k['total_known_pipeline_value']:,.0f}"),
        ("Work Orders", f"{k['total_work_orders']}"),
        ("Data Completeness", f"{k['data_completeness_pct']}%" if k["data_completeness_pct"] is not None else "—"),
    ]
    for col, (label, value) in zip(cols, cards):
        col.markdown(
            f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div></div>',
            unsafe_allow_html=True,
        )
    st.caption("↑ Live snapshot from monday.com, computed fresh when this page loaded.")
elif st.session_state.kpi_error:
    st.warning(f"Couldn't load live KPIs: {st.session_state.kpi_error}")

st.divider()

tab_chat, tab_dashboard = st.tabs(["💬 Chat", "📊 Dashboard"])

with tab_dashboard:
    if "charts" not in st.session_state:
        try:
            st.session_state.charts = get_dashboard_charts()
            st.session_state.charts_error = None
        except Exception as e:
            st.session_state.charts = None
            st.session_state.charts_error = str(e)

    if st.session_state.charts:
        c = st.session_state.charts
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Deals by stage")
            df = pd.DataFrame(c["deals_by_stage"]).set_index("label")
            st.bar_chart(df, horizontal=True)
        with col2:
            st.subheader("Known deal value by sector (Rs.)")
            df = pd.DataFrame(c["deal_value_by_sector"]).set_index("label")
            st.bar_chart(df, horizontal=True)

        col3, col4 = st.columns(2)
        with col3:
            st.subheader("Work orders by execution status")
            df = pd.DataFrame(c["work_orders_by_status"]).set_index("label")
            st.bar_chart(df, horizontal=True)
        with col4:
            st.subheader("Work orders by billing status")
            df = pd.DataFrame(c["work_orders_by_billing"]).set_index("label")
            st.bar_chart(df, horizontal=True)

        st.caption("↑ Computed live from monday.com the same way the chat agent's summary tools do.")
    elif st.session_state.charts_error:
        st.warning(f"Couldn't load dashboard charts: {st.session_state.charts_error}")

with st.sidebar:
    st.subheader("Quick actions")
    if st.button("📋 Prepare leadership update", use_container_width=True):
        st.session_state.pending_prompt = (
            "Prepare a leadership update covering: overall pipeline health "
            "(by stage and sector), operational/billing status from work "
            "orders, and a short list of data-quality caveats I should be "
            "aware of. Format it so I can paste it into an email or doc."
        )
    if st.button("🔄 Clear conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.raw_conversation = []
        st.rerun()

    st.divider()
    st.subheader("Try asking")
    examples = [
        "How's our pipeline looking for the energy sector this quarter?",
        "Which deals are missing a close date?",
        "How's our work order billing status?",
        "Which won deals have no matching work order yet?",
    ]
    for ex in examples:
        if st.button(ex, use_container_width=True, key=f"ex_{ex[:20]}"):
            st.session_state.pending_prompt = ex

    st.divider()
    st.caption("Reasoning: Groq (free tier) · Data: monday.com API, read-only")

for msg in st.session_state.messages:
    with tab_chat, st.chat_message(msg["role"]):
        st.markdown(_sanitize(msg["content"]))
        if msg.get("tools"):
            labels = ", ".join(TOOL_LABELS.get(t, t) for t in msg["tools"])
            st.markdown(f'<div class="tool-trace">🔎 Queried live: {labels}</div>', unsafe_allow_html=True)

with tab_chat:
    prompt = st.chat_input("Ask a business question...")
if "pending_prompt" in st.session_state:
    prompt = st.session_state.pop("pending_prompt")

if prompt:
    with tab_chat:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        st.session_state.raw_conversation.append({"role": "user", "content": prompt})

        with st.chat_message("assistant"):
            with st.spinner("Checking monday.com..."):
                reply, updated_conversation, tools_called = run_agent(st.session_state.raw_conversation, client)
            st.markdown(_sanitize(reply))
            if tools_called:
                labels = ", ".join(TOOL_LABELS.get(t, t) for t in tools_called)
                st.markdown(f'<div class="tool-trace">🔎 Queried live: {labels}</div>', unsafe_allow_html=True)

        st.session_state.raw_conversation = updated_conversation
        st.session_state.messages.append({"role": "assistant", "content": reply, "tools": tools_called})
