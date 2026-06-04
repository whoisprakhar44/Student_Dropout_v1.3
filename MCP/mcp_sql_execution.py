"""
sql_mcp_server.py — MCP server for SQL execution.

Tools:
  • execute_sql(query) : LLM-generated SQL → runs it, returns structured JSON
  • get_schema()       : returns all tables + columns for LLM context

Uses SQLAlchemy connection pool for production-grade connection management.

Environment variables (.env):
    SQLITE_DATABASE_PATH  if set, use local SQLite (local dev)
    MYSQL_USER      required when SQLite not set
    MYSQL_PASSWORD  required when SQLite not set
    MYSQL_HOST      default: localhost
    MYSQL_PORT      default: 3306
    MYSQL_DATABASE  required when SQLite not set
    MYSQL_POOL_SIZE default: 10
"""

try:
    import sys
    import pysqlite3
    sys.modules["sqlite3"] = pysqlite3
except ImportError:
    pass

import json
import logging
import os

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import re
# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP("sql-server")

# ---------------------------------------------------------------------------
# Engine (created once at startup)
# ---------------------------------------------------------------------------
# def _build_engine():
#     url = (
#         f"mysql+mysqlconnector://"
#         f"{os.getenv('MYSQL_USER')}:{os.getenv('MYSQL_PASSWORD')}"
#         f"@{os.getenv('MYSQL_HOST', 'localhost')}:{os.getenv('MYSQL_PORT', '3306')}"
#         f"/{os.getenv('MYSQL_DATABASE')}"
#     )
#     engine = create_engine(
#         url,
#         pool_size=int(os.getenv("MYSQL_POOL_SIZE", 10)),
#         max_overflow=20,        # extra connections allowed during traffic spikes
#         pool_pre_ping=True,     # silently replaces stale/dropped connections
#         pool_recycle=1800,      # recycle connections every 30 min
#     )
#     logger.info("SQLAlchemy engine created → %s", os.getenv("MYSQL_DATABASE"))
#     return engine
from urllib.parse import quote_plus

def _resolve_sqlite_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    mcp_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(mcp_dir, path))


def _build_engine():
    sqlite_path = os.getenv("SQLITE_DATABASE_PATH")
    if sqlite_path:
        sqlite_path = _resolve_sqlite_path(sqlite_path)
        url = f"sqlite:///{sqlite_path}"
        logger.info("SQLAlchemy engine created → SQLite %s", sqlite_path)
        return create_engine(url, connect_args={"check_same_thread": False})

    username = quote_plus(os.getenv("MYSQL_USER", ""))
    password = quote_plus(os.getenv("MYSQL_PASSWORD", ""))

    url = (
        f"mysql+mysqlconnector://"
        f"{username}:{password}"
        f"@{os.getenv('MYSQL_HOST', 'localhost')}"
        f":{os.getenv('MYSQL_PORT', '3306')}"
        f"/{os.getenv('MYSQL_DATABASE')}"
    )

    engine = create_engine(
        url,
        pool_size=int(os.getenv("MYSQL_POOL_SIZE", 10)),
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    logger.info("SQLAlchemy engine created → %s", os.getenv("MYSQL_DATABASE"))
    return engine

engine = _build_engine()


# ---------------------------------------------------------------------------
# Query Validation
# ---------------------------------------------------------------------------
def _validate_query(query: str) -> tuple[bool, str]:
    """
    Allow ONLY read-only SELECT queries.

    Supports:
    - SELECT ...
    - WITH cte AS (...) SELECT ...

    Blocks:
    - INSERT
    - UPDATE
    - DELETE
    - DROP
    - ALTER
    - CREATE
    - TRUNCATE
    - EXEC
    - Multiple statements (;)
    """

    if not query or not query.strip():
        return False, "Empty query"

    # Remove comments
    cleaned = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = cleaned.strip()

    lowered = cleaned.lower()

    # Block multiple statements
    # Allow only one optional trailing semicolon
    if ";" in cleaned[:-1]:
        return False, "Multiple SQL statements are not allowed"

    # Allowed start keywords
    allowed = ("select", "with")

    if not lowered.startswith(allowed):
        return False, "Only SELECT statements are allowed"

    # Dangerous keywords anywhere in query
    forbidden_keywords = [
        "insert",
        "update",
        "delete",
        "drop",
        "truncate",
        "alter",
        "create",
        "replace",
        "grant",
        "revoke",
        "commit",
        "rollback",
        "exec",
        "execute",
        "call",
        "lock",
        "unlock",
        "merge",
    ]

    pattern = r"\b(" + "|".join(forbidden_keywords) + r")\b"

    if re.search(pattern, lowered):
        return False, "Forbidden SQL keyword detected"

    return True, "Valid SELECT query"
# ---------------------------------------------------------------------------
# Tool: execute_sql
# ---------------------------------------------------------------------------
@mcp.tool()
def execute_sql(query: str) -> str:
    """
    Execute ONLY SELECT SQL queries.
    """

    logger.info("[execute_sql] %s", query[:200])

    # Validate query first
    is_valid, message = _validate_query(query)

    if not is_valid:
        logger.warning("[execute_sql] blocked query: %s", message)

        return json.dumps({
            "status": "error",
            "error_type": "validation_error",
            "error_msg": message,
            "query": query,
        })

    try:
        with engine.connect() as conn:
            result = conn.execute(text(query))

            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]

            return json.dumps(
                {
                    "status": "success",
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                },
                default=str,
            )

    except SQLAlchemyError as e:
        logger.error("[execute_sql] error: %s", e)

        return json.dumps({
            "status": "error",
            "error_type": "execution_error",
            "error_msg": str(e),
            "query": query,
        })


# ---------------------------------------------------------------------------
# Tool: get_schema
# ---------------------------------------------------------------------------
# @mcp.tool()
# def get_schema() -> str:
#     """
#     Introspect the database and return all table + column definitions.

#     Returns JSON:
#     {
#         "database": "my_db",
#         "tables": [
#             {
#                 "table": "users",
#                 "columns": [
#                     {"name": "id", "type": "int", "nullable": false, "key": "PRI"},
#                     ...
#                 ]
#             }
#         ]
#     }
#     """
#     db_name = os.getenv("MYSQL_DATABASE")
#     logger.info("[get_schema] introspecting %s", db_name)
#     try:
#         with engine.connect() as conn:
#             tables = conn.execute(text(
#                 "SELECT TABLE_NAME FROM information_schema.TABLES "
#                 "WHERE TABLE_SCHEMA = :db ORDER BY TABLE_NAME"
#             ), {"db": db_name}).fetchall()

#             schema = {"database": db_name, "tables": []}

#             for (table_name,) in tables:
#                 columns = conn.execute(text(
#                     "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY "
#                     "FROM information_schema.COLUMNS "
#                     "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :table "
#                     "ORDER BY ORDINAL_POSITION"
#                 ), {"db": db_name, "table": table_name}).fetchall()

#                 schema["tables"].append({
#                     "table": table_name,
#                     "columns": [
#                         {
#                             "name":     col[0],
#                             "type":     col[1],
#                             "nullable": col[2] == "YES",
#                             "key":      col[3] or None,
#                         }
#                         for col in columns
#                     ],
#                 })

#         logger.info("[get_schema] found %d table(s)", len(schema["tables"]))
#         return json.dumps(schema, indent=2)

#     except SQLAlchemyError as e:
#         logger.error("[get_schema] error: %s", e)
#         return json.dumps({"status": "error", "error_msg": str(e)})


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(
        transport="stdio",
    )