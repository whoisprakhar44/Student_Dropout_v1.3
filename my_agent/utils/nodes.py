"""
nodes.py
--------
LangGraph node functions for the SQL assistant.

The LLM decides whether to call schema RAG (`retrive_schema_rag`), SQL execution
(`execute_sql`), or answer directly. There is no deterministic retrieval node in
the graph.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_ollama import ChatOllama
from langgraph.prebuilt import ToolNode

from my_agent.utils import tools as tool_registry
from my_agent.utils.state import AgentState

logger = logging.getLogger("agent.nodes")

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen3.5:9b")
_REASONING = os.getenv("OLLAMA_REASONING", "true").strip().lower() in ("true", "1", "yes")
print(f"ChatOllama model: {_CHAT_MODEL}  |  thinking={'on' if _REASONING else 'off'}")

_base_model = ChatOllama(
    model=_CHAT_MODEL,
    temperature=0,
    reasoning=_REASONING,
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "4096")),
    num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "512")),
)
_model_with_tools = None

_HIVE_ENABLED = os.getenv("HIVE_MCP_ENABLED", "false").strip().lower() in ("true", "1", "yes")

if _HIVE_ENABLED:
    SYSTEM_PROMPT = """You are a SQL data assistant with a live Hive / Apache Spark SQL database for the curated_datamodels data model.

Available tools:
- retrive_schema_rag: retrieve curated table DDL and join relations when you need schema context.
- execute_sql: execute read-only Hive SQL SELECT queries against the database.

KEY COLUMNS (use these exact names — do NOT guess or invent column names):
- curated_datamodels.citizen_student: citizen_student_id_pk, student_name, gender, date_of_birth, social_category, current_grade, address, email_id, primary_mobile_no, citizen_school_id_fk, is_current
- curated_datamodels.citizen_school: citizen_school_id_pk, school_name, district_name, mandal_name, village_name, urban_rural_flag, functional_status, min_class, max_class, head_master_name
- curated_datamodels.school_student_attendance_fact: school_student_attendance_fact_id_pk, citizen_student_id_fk, student_school_id_fk, academic_year, present_flag, absent_flag, attendance_status_code
- curated_datamodels.school_academic_performance_fact: school_academic_performance_fact_id_pk, citizen_student_id_fk, citizen_school_id_fk, academic_year, marks_obtained, maximum_marks, percentage_score, pass_flag, fail_flag

KEY JOIN RELATIONSHIPS (use these exact columns for JOINs):
- To join students and schools: curated_datamodels.citizen_student.citizen_school_id_fk = curated_datamodels.citizen_school.citizen_school_id_pk
- To join attendance and students: curated_datamodels.school_student_attendance_fact.citizen_student_id_fk = curated_datamodels.citizen_student.citizen_student_id_pk
- To join attendance and schools: curated_datamodels.school_student_attendance_fact.student_school_id_fk = curated_datamodels.citizen_school.citizen_school_id_pk
- To join academic performance and students: curated_datamodels.school_academic_performance_fact.citizen_student_id_fk = curated_datamodels.citizen_student.citizen_student_id_pk
- To join academic performance and schools: curated_datamodels.school_academic_performance_fact.citizen_school_id_fk = curated_datamodels.citizen_school.citizen_school_id_pk

STRICT RULES — follow every rule without exception:
1. For ANY question about counts, totals, lists, averages, rates, trends, or data values — you MUST call execute_sql.
2. If you do not know the table name, call retrive_schema_rag first, then IMMEDIATELY call execute_sql with a SELECT query.
3. NEVER describe DDL or schema to the user — always run execute_sql and report the actual data.
4. NEVER answer without calling execute_sql for data questions.
5. After execute_sql returns rows, summarize the result in plain language.
6. When the user asks to show/list N rows, include LIMIT N and return the requested rows.
7. When the user asks to show students, schools, teachers, districts, or similar entities, select useful identifying columns, not only a count.
8. When the user asks for "top" without a metric, infer the most useful ranking from context; for schools, use student count unless another metric is named. Note: There is NO student_count, enrollment, or total_students column in curated_datamodels.citizen_school. You MUST join curated_datamodels.citizen_school and curated_datamodels.citizen_student on curated_datamodels.citizen_school.citizen_school_id_pk = curated_datamodels.citizen_student.citizen_school_id_fk, group by the school ID/name, use COUNT(curated_datamodels.citizen_student.citizen_student_id_pk) to calculate the student count, and order by that count descending.
9. For broad list requests without a requested row count, include LIMIT 20.
10. Core tables: curated_datamodels.citizen_student (students), curated_datamodels.citizen_school (schools), curated_datamodels.school_student_attendance_fact (attendance), curated_datamodels.school_academic_performance_fact (performance), curated_datamodels.scheme_benefits_fact, curated_datamodels.mid_day_meal_serving_fact, curated_datamodels.school_infrastructure_progress_fact.
11. The database is Hive - use Hive/Spark-compatible SQL only. Use the correct database prefix (e.g. write `curated_datamodels.citizen_student`).
"""
else:
    SYSTEM_PROMPT = """You are a SQL data assistant with a live SQLite sample database for the curated_datamodels school data model.

Available tools:
- retrive_schema_rag: retrieve curated table DDL and join relations when you need schema context.
- execute_sql: execute read-only SQLite SELECT queries against the sample database.

KEY COLUMNS (use these exact names — do NOT guess or invent column names):
- citizen_student: citizen_student_id_pk, student_name, gender, date_of_birth, social_category, current_grade, address, email_id, primary_mobile_no, citizen_school_id_fk, is_current
- citizen_school: citizen_school_id_pk, school_name, district_name, mandal_name, village_name, urban_rural_flag, functional_status, min_class, max_class, head_master_name
- school_student_attendance_fact: school_student_attendance_fact_id_pk, citizen_student_id_fk, student_school_id_fk, academic_year, present_flag, absent_flag, attendance_status_code
- school_academic_performance_fact: school_academic_performance_fact_id_pk, citizen_student_id_fk, citizen_school_id_fk, academic_year, marks_obtained, maximum_marks, percentage_score, pass_flag, fail_flag

KEY JOIN RELATIONSHIPS (use these exact columns for JOINs):
- To join students and schools: citizen_student.citizen_school_id_fk = citizen_school.citizen_school_id_pk
- To join attendance and students: school_student_attendance_fact.citizen_student_id_fk = citizen_student.citizen_student_id_pk
- To join attendance and schools: school_student_attendance_fact.student_school_id_fk = citizen_school.citizen_school_id_pk
- To join academic performance and students: school_academic_performance_fact.citizen_student_id_fk = citizen_student.citizen_student_id_pk
- To join academic performance and schools: school_academic_performance_fact.citizen_school_id_fk = citizen_school.citizen_school_id_pk

STRICT RULES — follow every rule without exception:
1. For ANY question about counts, totals, lists, averages, rates, trends, or data values — you MUST call execute_sql.
2. If you need columns not listed above, call retrive_schema_rag first, then IMMEDIATELY call execute_sql.
3. NEVER describe DDL or schema to the user — always run execute_sql and report the actual data.
4. NEVER answer without calling execute_sql for data questions.
5. After execute_sql returns rows, summarize the result in plain language.
6. When the user asks to show/list N rows, include LIMIT N and return the requested rows.
7. When the user asks to show students, schools, teachers, districts, or similar entities, select useful identifying columns, not only a count.
8. When the user asks for "top" without a metric, infer the most useful ranking from context; for schools, use student count unless another metric is named. Note: There is NO student_count, enrollment, or total_students column in citizen_school. You MUST join citizen_school and citizen_student on citizen_school.citizen_school_id_pk = citizen_student.citizen_school_id_fk, group by the school ID/name, use COUNT(citizen_student.citizen_student_id_pk) to calculate the student count, and order by that count descending.
9. For broad list requests without a requested row count, include LIMIT 20.
10. Core tables: citizen_student (students), citizen_school (schools), school_student_attendance_fact (attendance), school_academic_performance_fact (performance), scheme_benefits_fact, mid_day_meal_serving_fact, school_infrastructure_progress_fact.
11. The database is SQLite - use SQLite-compatible SQL only. All tables are in the main schema with no prefix (e.g. write `citizen_student` instead of `curated_datamodels.citizen_student`).
"""


def _get_model():
    global _model_with_tools
    if _model_with_tools is None:
        if not tool_registry.execution_tools:
            raise RuntimeError(
                "Tools not loaded. Make sure init_tools() was awaited before compiling the graph."
            )
        _model_with_tools = _base_model.bind_tools(tool_registry.execution_tools)
    return _model_with_tools


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.lower().replace("data base", "database"))


def _needs_data_tool(query: str) -> bool:
    q = _normalize_query(query)
    triggers = (
        "how many", "count", "number of", "total", "list", "show", "what is",
        "average", "avg", "percent", "rate", "trend", "chart", "pie", "bar",
        "heatmap", "student", "teacher", "gender", "district", "school",
        "attendance", "absent", "absence", "marks", "score", "risk", "scheme",
        "meal", "infrastructure", "database", "table",
    )
    return any(trigger in q for trigger in triggers)


def _tool_messages(messages: list, name: str | None = None) -> list:
    out = [
        m for m in messages
        if isinstance(m, ToolMessage) or getattr(m, "__class__", None).__name__ == "ToolMessage"
    ]
    if name:
        out = [m for m in out if getattr(m, "name", None) == name]
    return out


def _extract_tool_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                return item
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text")
    return str(content)


def _summarize_sql_result(user_query: str, tool_content: Any) -> str | None:
    text_content = _extract_tool_content(tool_content)
    if not text_content:
        return None
    try:
        payload = json.loads(text_content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None

    rows = payload.get("rows") or []
    columns = payload.get("columns") or []
    if not rows:
        return "The query ran successfully but returned no rows."

    q = _normalize_query(user_query)
    if len(rows) == 1 and len(columns) == 1:
        val = rows[0].get(columns[0])
        if re.search(r"how many|count|number of|total|average|avg", q):
            label = columns[0].replace("_", " ")
            return f"**{val:,}** ({label})." if isinstance(val, (int, float)) else f"**{val}** ({label})."

    if len(rows) <= 15 and columns:
        header = " | ".join(columns)
        body = "\n".join(
            " | ".join(str(row.get(column, "")) for column in columns)
            for row in rows[:15]
        )
        extra = ""
        if len(rows) < payload.get("row_count", len(rows)):
            extra = f"\n\n_Showing {len(rows)} of {payload.get('row_count', len(rows))} rows._"
        return f"**Query results:**\n\n{header}\n{body}{extra}"

    return (
        f"Query returned **{payload.get('row_count', len(rows))}** rows "
        f"({', '.join(columns[:6])}{'...' if len(columns) > 6 else ''})."
    )


def llm_node(state: AgentState) -> dict:
    """
    Invoke the LLM. The LLM may call schema retrieval, execute SQL, or answer.
    Retry guards nudge data questions back to tools if the model answers without
    a tool call, or if it called SQL with wrong columns then retrieved schema.
    """
    t0 = time.perf_counter()
    history = state.get("messages", [])
    if not history:
        history = [HumanMessage(content=state["user_query"])]

    # Hard cap on LLM calls to prevent infinite loops
    current_calls = state.get("llm_calls", 0)
    max_llm_calls = int(os.getenv("MAX_LLM_CALLS", "15"))
    if current_calls >= max_llm_calls:
        # Collect the last SQL error message (if any) to include in the fallback.
        last_sql_error: str | None = None
        for m in reversed(_tool_messages(history, "execute_sql")):
            try:
                text_content = _extract_tool_content(m.content)
                if text_content:
                    err_payload = json.loads(text_content)
                    if err_payload.get("status") == "error":
                        last_sql_error = err_payload.get("error_msg") or err_payload.get("error_type")
                        break
            except (json.JSONDecodeError, TypeError, AttributeError):
                break

        logger.warning("llm_node: Max LLM call limit reached (%d). Ending conversation.", current_calls)
        error_hint = f" (Last error: {last_sql_error})" if last_sql_error else ""
        return {
            "messages": [AIMessage(content=f"I encountered multiple issues or errors while trying to query the database. Please try rephrasing your request.{error_hint}")],
            "llm_calls": current_calls,
        }

    # If a successful SQL result already exists in history, summarise and stop —
    # BUT only if we are not currently in a verification retry cycle.
    # When verify_calls > 0 and verified is still False the verify_node sent us
    # back here to regenerate SQL, so we must NOT short-circuit.
    in_verify_retry = state.get("verify_calls", 0) > 0 and not state.get("verified", False)
    sql_results = _tool_messages(history, "execute_sql")
    if sql_results and not in_verify_retry:
        summary = _summarize_sql_result(state["user_query"], sql_results[-1].content)
        if summary:
            logger.info("llm_node: summarized SQL result in %.2fs", time.perf_counter() - t0)
            return {
                "messages": [AIMessage(content=summary)],
                "llm_calls": current_calls,
            }

    system_message = SystemMessage(content=SYSTEM_PROMPT)
    messages_for_llm = [system_message] + history
    response = _get_model().invoke(messages_for_llm)
    llm_steps = 1

    # Retry 1: model answered without calling any tool at all.
    if (
        _needs_data_tool(state["user_query"])
        and not getattr(response, "tool_calls", None)
        and not _tool_messages(history)
    ):
        retry_hint = HumanMessage(
            content=(
                "This is a database question. You MUST call retrive_schema_rag first "
                "if you don't know the table, then call execute_sql. "
                "Do NOT answer without running SQL."
            )
        )
        response = _get_model().invoke(messages_for_llm + [retry_hint])
        llm_steps += 1

    # Retry 2: RAG was retrieved but there is still no SUCCESSFUL execute_sql.
    # Covers two cases:
    #   a) RAG called, SQL never attempted → nudge to run SQL now.
    #   b) SQL attempted with wrong columns (error), then RAG fetched schema →
    #      nudge to retry SQL using the retrieved column names.
    rag_results = _tool_messages(history, "retrive_schema_rag")
    successful_sql = [
        m for m in _tool_messages(history, "execute_sql")
        if _summarize_sql_result(state["user_query"], m.content) is not None
    ]

    # Collect the last SQL error message (if any) to include in the nudge.
    last_sql_error: str | None = None
    for m in reversed(_tool_messages(history, "execute_sql")):
        try:
            text_content = _extract_tool_content(m.content)
            if text_content:
                err_payload = json.loads(text_content)
                if err_payload.get("status") == "error":
                    last_sql_error = err_payload.get("error_msg") or err_payload.get("error_type")
                    break
        except (json.JSONDecodeError, TypeError, AttributeError):
            break

    if (
        rag_results
        and not successful_sql
        and not getattr(response, "tool_calls", None)
        and _needs_data_tool(state["user_query"])
    ):
        db_type = "Hive" if _HIVE_ENABLED else "SQLite"
        error_hint = (
            f" The previous SQL failed: {last_sql_error}."
            " Use the exact column names from the schema you just retrieved."
            if last_sql_error else ""
        )
        sql_nudge = HumanMessage(
            content=(
                f'The user asked: "{state["user_query"]}"\n\n'
                "You have already retrieved the schema context above."
                f"{error_hint} "
                f"Now call execute_sql with a valid {db_type} SELECT query "
                "using the exact column names shown in the schema. "
                "Do NOT describe the schema — call execute_sql right now."
            )
        )
        response = _get_model().invoke(messages_for_llm + [sql_nudge])
        llm_steps += 1

    logger.info("llm_node: completed in %.2fs", time.perf_counter() - t0)
    return {
        "messages": [response],
        "llm_calls": state.get("llm_calls", 0) + llm_steps,
    }


def build_tool_node() -> ToolNode:
    """Returns a ToolNode bound to SQL and RAG MCP tools."""
    if not tool_registry.execution_tools:
        raise RuntimeError("Tools not loaded before building tool node.")
    return ToolNode(tool_registry.execution_tools)


# ---------------------------------------------------------------------------
# Verification node
# ---------------------------------------------------------------------------
_MAX_VERIFY_LOOPS = int(os.getenv("MAX_VERIFY_LOOPS", "10"))

_VERIFY_PROMPT = """You are a strict SQL result verifier.

User question: {user_query}

SQL query that was executed:
{sql}

Query result (up to 20 rows shown):
{result_table}

Does this result correctly and completely answer the user's question?

Reply with EXACTLY one of:
  CORRECT: <one-sentence explanation of why the result is correct>
  RETRY: <one-sentence explanation of what is wrong and how to fix the SQL>

Do NOT output anything else."""


def _extract_sql_from_history(history: list) -> str:
    """Extract the most recently executed SQL string from AIMessage tool_calls."""
    for m in reversed(history):
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            if tc.get("name") == "execute_sql":
                args = tc.get("args", {})
                return args.get("query") or args.get("sql") or ""
    return ""


def _result_table_str(tool_content: Any, max_rows: int = 20) -> str:
    """Render the SQL result as a plain-text table for the verifier prompt."""
    text_content = _extract_tool_content(tool_content)
    if not text_content:
        return "(no result)"
    try:
        payload = json.loads(text_content)
    except (json.JSONDecodeError, TypeError):
        return text_content[:500]
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return f"(error) {payload.get('error_msg', '')}"
    rows = payload.get("rows") or []
    columns = payload.get("columns") or []
    if not rows:
        return "(query returned 0 rows)"
    header = " | ".join(columns)
    body = "\n".join(
        " | ".join(str(row.get(c, "")) for c in columns)
        for row in rows[:max_rows]
    )
    suffix = ""
    total = payload.get("row_count", len(rows))
    if total > max_rows:
        suffix = f"\n(showing {max_rows} of {total} rows)"
    return f"{header}\n{body}{suffix}"


def verify_node(state: AgentState) -> dict:
    """
    Ask the LLM whether the most recent SQL result correctly answers the user
    question. Up to _MAX_VERIFY_LOOPS rounds are allowed.

    Verdict CORRECT  → emit the final answer and set verified=True.
    Verdict RETRY    → inject a corrective HumanMessage so the next llm_node
                       turn rewrites and re-executes the SQL.
    """
    t0 = time.perf_counter()
    history = state.get("messages", [])
    verify_calls = state.get("verify_calls", 0)

    # Find the latest successful SQL result
    successful = [
        m for m in _tool_messages(history, "execute_sql")
        if _summarize_sql_result(state["user_query"], m.content) is not None
    ]

    # Guard: no SQL execution was even attempted (e.g. LLM answered directly)
    sql_attempts = _tool_messages(history, "execute_sql")
    if not sql_attempts:
        logger.info("verify_node: no SQL query was executed; skipping verification")
        return {
            "messages": [AIMessage(content="No query result was returned.")],
            "verify_calls": verify_calls + 1,
            "verified": True,
        }

    # If SQL was attempted, but none were successful, we have a failed SQL execution
    if not successful:
        last_attempt = sql_attempts[-1]
        try:
            text_content = _extract_tool_content(last_attempt.content)
            payload = json.loads(text_content) if text_content else {}
            error_msg = payload.get("error_msg") or "Unknown execution error"
        except (json.JSONDecodeError, TypeError, ValueError):
            error_msg = "Unknown execution error"
            
        sql = _extract_sql_from_history(history)
        
        correction_msg = HumanMessage(
            content=(
                f"The previous SQL query failed to execute with the following error:\n"
                f"{error_msg}\n\n"
                f"The SQL that was run:\n{sql}\n\n"
                "Please correct the query syntax or column names, verify them against the schema, "
                "and call execute_sql again with the corrected query."
            )
        )
        logger.warning(
            "verify_node: SQL failed execution (error: %s). Routing back to LLM for correction.",
            error_msg,
        )
        return {
            "messages": [correction_msg],
            "verify_calls": verify_calls + 1,
            "verified": False,
        }

    last_result_msg = successful[-1]
    sql = _extract_sql_from_history(history)
    result_table = _result_table_str(last_result_msg.content)

    # Hard cap: if we have exhausted all verify loops, accept the current result
    if verify_calls >= _MAX_VERIFY_LOOPS:
        logger.warning(
            "verify_node: max verification loops (%d) reached; accepting result as-is",
            _MAX_VERIFY_LOOPS,
        )
        summary = _summarize_sql_result(state["user_query"], last_result_msg.content)
        return {
            "messages": [AIMessage(content=summary or result_table)],
            "verify_calls": verify_calls + 1,
            "verified": True,
        }

    # Build and call the verifier
    verifier_prompt = _VERIFY_PROMPT.format(
        user_query=state["user_query"],
        sql=sql or "(SQL not captured)",
        result_table=result_table,
    )
    verdict_response = _base_model.invoke([
        SystemMessage(content=(
            "You are a strict SQL result verifier. "
            "Your only job is to decide whether a SQL query result correctly and completely answers the user's question. "
            "Reply with EXACTLY 'CORRECT: <reason>' or 'RETRY: <reason>'. No other output."
        )),
        HumanMessage(content=verifier_prompt),
    ])
    verdict_text = (verdict_response.content or "").strip()
    logger.info(
        "verify_node [round %d]: verdict=%s  (%.2fs)",
        verify_calls + 1,
        verdict_text[:80],
        time.perf_counter() - t0,
    )

    if verdict_text.upper().startswith("CORRECT"):
        # Result is verified — produce the final pretty summary
        summary = _summarize_sql_result(state["user_query"], last_result_msg.content)
        explanation = verdict_text.split(":", 1)[-1].strip() if ":" in verdict_text else ""
        final_answer = summary or result_table
        if explanation:
            final_answer += f"\n\n✅ *Verified: {explanation}*"
        logger.info("verify_node: result verified as CORRECT in round %d", verify_calls + 1)
        return {
            "messages": [AIMessage(content=final_answer)],
            "verify_calls": verify_calls + 1,
            "verified": True,
        }

    # RETRY path — extract the reason and inject a corrective instruction
    retry_reason = verdict_text.split(":", 1)[-1].strip() if ":" in verdict_text else verdict_text
    correction_msg = HumanMessage(
        content=(
            f'The previous SQL result did NOT correctly answer: "{state["user_query"]}"\n'
            f"Reason: {retry_reason}\n\n"
            f"The SQL that was run:\n{sql}\n\n"
            "Please call execute_sql again with a corrected query that fixes the issue described above."
        )
    )
    logger.info("verify_node: RETRY round %d — injecting correction", verify_calls + 1)
    return {
        "messages": [correction_msg],
        "verify_calls": verify_calls + 1,
        "verified": False,
    }
