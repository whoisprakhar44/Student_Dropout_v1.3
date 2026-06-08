"""
mcp_hive_execution.py
─────────────────────
MCP server for Impala SQL execution (Kerberos/GSSAPI auth).

The class and file names are preserved for backward compatibility.
Internally this server uses HiveExecutor backed by Impyla + Impala.

Activated when HIVE_MCP_ENABLED=true in .env.

Tool exposed (same contract as mcp_sql_execution.py):
    execute_sql(query: str) -> str   JSON result

JSON contract
─────────────
Success:
    { "status": "success", "columns": [...], "rows": [...], "row_count": N }

Error:
    { "status": "error", "error_type": "...", "error_msg": "...", "query": "..." }

Startup behaviour
─────────────────
• If HIVE_MCP_ENABLED != "true"  → server starts but returns a clear
  "not_configured" error for every query (safe for local SQLite dev).
• If HIVE_MCP_ENABLED == "true"  → HiveExecutor is initialised at import
  time; if config or env is broken the process exits immediately (fail fast).
"""

import json
import logging
import os
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("impala-mcp")

# ─────────────────────────────────────────────────────────────────────────────
#  Resolve config path relative to this file (works regardless of CWD)
# ─────────────────────────────────────────────────────────────────────────────

_HERE        = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_HERE, "hive_config.yaml")

# ─────────────────────────────────────────────────────────────────────────────
#  Conditional initialisation
# ─────────────────────────────────────────────────────────────────────────────

_HIVE_ENABLED = os.getenv("HIVE_MCP_ENABLED", "false").strip().lower() in ("true", "1", "yes")
_executor = None

if _HIVE_ENABLED:
    try:
        # HiveExecutor name is preserved for backward compatibility;
        # it connects to Impala internally via Impyla.
        from hive_executor import HiveExecutor  # type: ignore[import]
        _executor = HiveExecutor(_CONFIG_PATH)
        logger.info("HiveExecutor (Impala) ready — MCP server active")
    except Exception as exc:
        logger.critical("Failed to initialise HiveExecutor: %s", exc, exc_info=True)
        sys.exit(1)   # fail fast — broken config should surface immediately
else:
    logger.info(
        "HIVE_MCP_ENABLED is not set to true — "
        "Impala execution disabled (local SQLite mode active)"
    )

# ─────────────────────────────────────────────────────────────────────────────
#  MCP server
# ─────────────────────────────────────────────────────────────────────────────

mcp = FastMCP("impala-sql-server")


@mcp.tool()
def execute_sql(query: str) -> str:
    """
    Execute a read-only Impala SQL SELECT query against the CDP cluster.

    Requires HIVE_MCP_ENABLED=true and a valid Kerberos ticket (kinit).
    Supports Iceberg tables via Impala — no Hive compatibility issues.

    Returns JSON:
        success → { status, columns, rows, row_count }
        error   → { status, error_type, error_msg, query }
    """
    if not _HIVE_ENABLED or _executor is None:
        return json.dumps({
            "status":     "error",
            "error_type": "not_configured",
            "error_msg": (
                "Impala execution is disabled. "
                "Set HIVE_MCP_ENABLED=true in .env and ensure the server has "
                "a valid Kerberos ticket and Impala access."
            ),
            "query": query,
        })

    return _executor.execute(query)


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
