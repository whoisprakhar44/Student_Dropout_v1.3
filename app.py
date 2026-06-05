try:
    import sys
    import pysqlite3
    sys.modules["sqlite3"] = pysqlite3
except ImportError:
    pass

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import sqlite3
import traceback

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from create_schema import create_database
from my_agent.agent import build_graph
from my_agent.utils.ollama_check import chat_model_name, check_ollama
from my_agent.utils.tools import cleanup_tools


DB_PATH = os.path.join(os.path.dirname(__file__), "database", "schema.db")


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural-language question.")


class AskResponse(BaseModel):
    sql: str
    result: list[dict[str, Any]]


def init_database() -> None:
    """Initialize curated sample database if missing or still on the old schema."""
    should_create = not os.path.exists(DB_PATH)
    if not should_create:
        try:
            conn = sqlite3.connect(DB_PATH)
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            conn.close()
            should_create = "citizen_student" not in tables or "students" in tables
        except sqlite3.Error:
            should_create = True

    if should_create:
        create_database(DB_PATH, replace=True)
        print(f"Curated database initialized at {DB_PATH}")


def _http_error_from_exc(exc: Exception) -> HTTPException:
    msg = str(exc)
    model = chat_model_name()
    if "not found" in msg.lower() and "model" in msg.lower():
        return HTTPException(
            status_code=503,
            detail=(
                f"Ollama model '{model}' is not installed. "
                f"Run: ollama pull {model} - then restart uvicorn. ({msg})"
            ),
        )
    return HTTPException(status_code=500, detail=msg)


def _extract_tool_content(message: Any) -> str | None:
    """Extract plain-text content from a ToolMessage.

    MCP adapters may deliver content as a plain string OR as a list of
    content blocks (e.g. [{"type": "text", "text": "..."}]).  Handle both.
    """
    raw = getattr(message, "content", None)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                return item
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text")
    return str(raw)


def _extract_sql_and_result(messages: list[Any]) -> AskResponse:
    from langchain_core.messages import ToolMessage as LCToolMessage

    sql = None
    result: list[dict[str, Any]] | None = None

    for message in messages:
        # ── extract SQL from any AIMessage tool call ──────────────────────────
        for tool_call in getattr(message, "tool_calls", None) or []:
            args = tool_call.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if tool_call.get("name") == "execute_sql" and args.get("query"):
                sql = args["query"]

        # ── extract rows from any execute_sql ToolMessage ─────────────────────
        is_tool_msg = isinstance(message, LCToolMessage) or (
            message.__class__.__name__ == "ToolMessage"
        )
        if not is_tool_msg:
            continue

        tool_name = getattr(message, "name", None)
        if tool_name != "execute_sql":
            continue

        raw_content = _extract_tool_content(message)
        if not raw_content:
            continue

        try:
            payload = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        if payload.get("status") == "success":
            result = payload.get("rows") or []

    if not sql or result is None:
        raise HTTPException(
            status_code=502,
            detail="The agent did not return an executed SQL query.",
        )

    return AskResponse(sql=sql, result=result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    ollama_status = check_ollama()
    app.state.ollama_status = ollama_status
    app.state.graph = None
    app.state.graph_lock = asyncio.Lock()
    if not ollama_status.get("model_available"):
        print("WARNING: Ollama chat model not available:", ollama_status)
    else:
        print("Ollama ready:", ollama_status.get("model"))

    yield
    if app.state.graph is not None:
        await cleanup_tools()


async def _get_graph():
    if app.state.graph is None:
        async with app.state.graph_lock:
            if app.state.graph is None:
                app.state.graph = await build_graph()
    return app.state.graph


app = FastAPI(
    title="Curated Datamodels API",
    version="1.0.0",
    description="API-only backend for natural-language SQL over curated sample data.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    info = check_ollama()
    return {
        "status": "ok" if info.get("model_available") else "degraded",
        **info,
    }


@app.post("/ask", response_model=AskResponse)
async def ask(payload: AskRequest):
    try:
        if not check_ollama().get("model_available"):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Ollama model '{chat_model_name()}' is not available. "
                    f"Run: ollama pull {chat_model_name()} - then restart uvicorn."
                ),
            )

        graph = await _get_graph()
        state = await graph.ainvoke(
            {
                "user_query": payload.question,
                "messages": [HumanMessage(content=payload.question)],
                "retrieved_context": [],
                "llm_calls": 0,
                "verify_calls": 0,
                "verified": False,
            }
        )
        return _extract_sql_and_result(state.get("messages", []))
    except HTTPException:
        raise
    except Exception as exc:
        traceback.print_exc()
        raise _http_error_from_exc(exc)
