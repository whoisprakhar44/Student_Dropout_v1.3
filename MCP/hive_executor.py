"""
hive_executor.py
────────────────
Reusable HiveExecutor class for production Impala/Iceberg query execution.

The class name HiveExecutor is preserved for backward compatibility with all
existing imports (mcp_hive_execution.py and any downstream consumers).
Internally the executor is a pure Impala client — no PyHive code remains.

Features
────────
  • Config-driven — all infra values come from hive_config.yaml (query_engine:)
  • Kerberos authentication via Impyla (GSSAPI)
  • Lazy-connect + auto-reconnect on failure
  • Optional session settings applied once per connection (and after reconnect)
  • SQL validation (SELECT-only; blocks DML/DDL even inside CTEs)
  • Timeout with cursor.cancel() — cancels the server-side query, not just the thread
  • Structured JSON results matching the existing MCP contract
  • health_check() with four sub-checks:
      1. Kerberos ticket (klist)
      2. Impala TCP connectivity
      3. SELECT 1 / SHOW DATABASES
      4. Iceberg table read (curated_datamodels.citizen_student LIMIT 1)

Impyla / Impala type handling
─────────────────────────────
Impyla natively returns:
  • datetime.date      for DATE columns
  • datetime.datetime  for TIMESTAMP columns
  • str                for STRING columns
  • int                for BIGINT / INT columns
No post-fetch type casting or driver patches are required.

JSON result contract (matches mcp_sql_execution.py)
────────────────────────────────────────────────────
  success → { status, columns, rows, row_count }
  error   → { status, error_type, error_msg, query }
"""

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("hive_executor")


# ─────────────────────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(config_path: str | None = None) -> dict:
    candidates = []
    if config_path:
        candidates.append(Path(config_path))
    candidates.append(Path(__file__).parent / "hive_config.yaml")
    candidates.append(Path.cwd() / "hive_config.yaml")

    for p in candidates:
        if p.exists():
            with open(p) as f:
                cfg = yaml.safe_load(f)
            logger.info("Loaded config from: %s", p)
            return cfg

    raise FileNotFoundError(
        "hive_config.yaml not found. Searched: "
        + ", ".join(str(p) for p in candidates)
    )


def _get_engine_cfg(cfg: dict) -> dict:
    """
    Return the query engine config block.

    Reads `query_engine:` as the canonical key.
    Falls back to `hive:` for any legacy config files encountered during
    migration — this fallback can be removed once all deployments are updated.
    """
    if "query_engine" in cfg:
        return cfg["query_engine"]
    if "hive" in cfg:
        logger.warning(
            "Config uses deprecated 'hive:' key — please rename to 'query_engine:'. "
            "Falling back for this session."
        )
        # Normalise legacy hive: block into query_engine shape
        hive = cfg["hive"]
        return {
            "type": "impala",
            "host": hive.get("host"),
            "port": hive.get("port", 21050),
            "database": hive.get("database", "curated_datamodels"),
            "kerberos": {
                "service_name": hive.get("kerberos_service_name", "impala"),
            },
        }
    raise KeyError(
        "Config must contain a 'query_engine:' section. "
        "See hive_config.yaml for the required structure."
    )


# ─────────────────────────────────────────────────────────────────────────────
#  SQL Validation
#  Strategy: block forbidden keywords anywhere in the query, regardless of
#  whether they appear inside a CTE body, after a WITH clause, etc.
#  The validator does NOT rely on the first keyword alone.
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN_KEYWORDS = [
    # DML
    "insert", "update", "delete", "merge",
    # DDL
    "create", "drop", "truncate", "alter",
    # Hive-specific DDL / admin
    "msck", "repair", "load",
    # Privilege / session
    "replace", "grant", "revoke",
    # Transaction / proc
    "commit", "rollback", "exec", "call", "lock", "unlock",
]

_FORBIDDEN_PATTERN = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _validate_query(query: str) -> tuple[bool, str]:
    """
    Allow only read-only SELECT / WITH…SELECT queries.

    Checks:
      1. Non-empty after stripping comments.
      2. Starts with SELECT or WITH.
      3. No forbidden keyword anywhere in the query body
         (catches  WITH x AS (...) INSERT INTO ...).
      4. No multiple statements (more than one semicolon).

    Returns (is_valid, reason).
    """
    if not query or not query.strip():
        return False, "Empty query"

    # Strip SQL comments before checking
    cleaned = re.sub(r"--.*?$", "", query, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL).strip()

    if not cleaned:
        return False, "Query is empty after stripping comments"

    # Only one statement (single trailing semicolon allowed)
    if ";" in cleaned[:-1]:
        return False, "Multiple SQL statements are not allowed"

    lowered = cleaned.lower()

    if not lowered.startswith(("select", "with")):
        return False, "Only SELECT queries are allowed"

    # Scan the full query body — blocks DML even inside CTE bodies
    match = _FORBIDDEN_PATTERN.search(lowered)
    if match:
        return False, f"Forbidden SQL keyword detected: '{match.group()}'"

    return True, "OK"


# ─────────────────────────────────────────────────────────────────────────────
#  HiveExecutor
#  (Name kept for backward-compatible imports; internally a pure Impala client)
# ─────────────────────────────────────────────────────────────────────────────

class HiveExecutor:
    """
    Production Impala query executor with Kerberos authentication.

    Class name preserved for backward compatibility with existing imports.

    Usage
    ─────
        executor = HiveExecutor()                       # loads hive_config.yaml
        executor = HiveExecutor("path/to/config.yaml")

        result_json = executor.execute(
            "SELECT COUNT(*) FROM curated_datamodels.citizen_student"
        )
    """

    def __init__(self, config_path: str | None = None):
        self._cfg      = _load_config(config_path)
        self._eng_cfg  = _get_engine_cfg(self._cfg)
        self._exec_cfg = self._cfg.get("execution", {})
        self._timeout  = int(self._exec_cfg.get("query_timeout_seconds", 300))
        self._conn     = None
        self._lock     = threading.Lock()

        # Resolved connection parameters
        self.host                 = self._eng_cfg["host"]
        self.port                 = int(self._eng_cfg.get("port", 21050))
        self.database             = self._eng_cfg.get("database", "curated_datamodels")
        self.kerberos_service_name = (
            self._eng_cfg.get("kerberos", {}).get("service_name", "impala")
        )

        # Optional Impala session SET statements (empty list = no-op)
        self._session_settings: list[str] = self._exec_cfg.get("session_settings") or []

        logger.info(
            "HiveExecutor (Impala) init — host=%s port=%s kerberos_service=%s "
            "timeout=%ss session_settings=%s",
            self.host,
            self.port,
            self.kerberos_service_name,
            self._timeout,
            self._session_settings,
        )

    # ── Connection management ─────────────────────────────────────────────────

    def _connect(self):
        from impala.dbapi import connect  # type: ignore[import]

        logger.info(
            "[connect] Connecting to Impala %s:%s (auth=GSSAPI service=%s)",
            self.host,
            self.port,
            self.kerberos_service_name,
        )
        self._conn = connect(
            host=self.host,
            port=self.port,
            auth_mechanism="GSSAPI",
            kerberos_service_name=self.kerberos_service_name,
        )
        logger.info("[connect] Impala connection established")

        # Apply session-scoped settings immediately after every new connection.
        self._apply_session_settings()

    def _apply_session_settings(self) -> None:
        """
        Execute all configured session_settings statements on the current
        connection.  Called once after _connect() — including after every
        auto-reconnect — so settings are always active for the lifetime of
        the connection.

        Errors are logged as WARNING rather than raised so that a mis-typed
        SET statement does not crash the server — queries will still execute,
        just without that particular setting applied.
        """
        if not self._session_settings or self._conn is None:
            return

        cursor = self._conn.cursor()
        try:
            for stmt in self._session_settings:
                try:
                    logger.info("[session] Applying: %s", stmt)
                    cursor.execute(stmt)
                    logger.info("[session] Applied:  %s", stmt)
                except Exception as exc:
                    logger.warning(
                        "[session] Failed to apply %r: %s — "
                        "queries will still execute but setting may be inactive",
                        stmt,
                        exc,
                    )
        finally:
            try:
                cursor.close()
            except Exception:
                pass

    def _get_connection(self):
        if self._conn is None:
            self._connect()
        return self._conn

    def _reset_connection(self):
        """Close the current connection and mark it as gone so the next call
        to _get_connection() triggers a fresh _connect() — which will also
        re-apply all session settings automatically."""
        logger.info("[reconnect] Resetting Impala connection")
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None
        logger.info("[reconnect] Connection reset complete — will reconnect on next query")

    # ── Health check ──────────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """
        Run four diagnostic checks and return a structured result dict.

        Returns
        ───────
        {
            "kerberos":     {"ok": bool, "detail": str},
            "impala_tcp":   {"ok": bool, "detail": str},
            "database":     {"ok": bool, "detail": str},
            "iceberg_read": {"ok": bool, "detail": str},
            "all_ok":       bool,
        }

        Sub-checks
        ──────────
        kerberos     — runs `klist`, parses the default principal line
        impala_tcp   — TCP socket-connect to host:port (no Impyla dependency)
        database     — opens a fresh cursor, runs SELECT 1 and SHOW DATABASES
        iceberg_read — runs SELECT * FROM curated_datamodels.citizen_student LIMIT 1
                       to validate end-to-end Iceberg read via Impala
        """
        result: dict[str, Any] = {}

        # ── 1. Kerberos ticket ────────────────────────────────────────────────
        logger.info("[health] Checking Kerberos ticket")
        kerberos_ok, kerberos_detail = self._check_kerberos()
        result["kerberos"] = {"ok": kerberos_ok, "detail": kerberos_detail}
        if kerberos_ok:
            logger.info("[health] Kerberos OK — %s", kerberos_detail)
        else:
            logger.warning("[health] Kerberos FAILED — %s", kerberos_detail)

        # ── 2. Impala TCP ─────────────────────────────────────────────────────
        logger.info("[health] Checking Impala TCP connectivity")
        tcp_ok, tcp_detail = self._check_impala_tcp()
        result["impala_tcp"] = {"ok": tcp_ok, "detail": tcp_detail}
        if tcp_ok:
            logger.info("[health] Impala TCP OK — %s", tcp_detail)
        else:
            logger.warning("[health] Impala TCP FAILED — %s", tcp_detail)

        # ── 3. SELECT 1 + SHOW DATABASES ─────────────────────────────────────
        logger.info("[health] Checking Impala database access")
        db_ok, db_detail = self._check_database()
        result["database"] = {"ok": db_ok, "detail": db_detail}
        if db_ok:
            logger.info("[health] Database OK — %s", db_detail)
        else:
            logger.error("[health] Database FAILED — %s", db_detail)

        # ── 4. Iceberg read validation ────────────────────────────────────────
        logger.info("[health] Checking Iceberg table read via Impala")
        ice_ok, ice_detail = self._check_iceberg_read()
        result["iceberg_read"] = {"ok": ice_ok, "detail": ice_detail}
        if ice_ok:
            logger.info("[health] Iceberg read OK — %s", ice_detail)
        else:
            logger.error("[health] Iceberg read FAILED — %s", ice_detail)

        result["all_ok"] = all(
            result[k]["ok"] for k in ("kerberos", "impala_tcp", "database", "iceberg_read")
        )
        logger.info("[health] Health check complete — all_ok=%s", result["all_ok"])
        return result

    def _check_kerberos(self) -> tuple[bool, str]:
        """Validate Kerberos ticket via klist subprocess."""
        klist = shutil.which("klist")
        if not klist:
            return False, "klist not found on PATH — krb5-workstation not installed"
        try:
            proc = subprocess.run(
                [klist],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            return False, "klist timed out after 10s"
        except Exception as exc:
            return False, f"klist failed to run: {exc}"

        if proc.returncode != 0:
            return (
                False,
                f"No valid Kerberos ticket (klist exit {proc.returncode}). "
                f"Run: kinit <principal>. stderr: {proc.stderr.strip()}",
            )

        # Extract principal for display
        principal = ""
        for line in proc.stdout.splitlines():
            if "principal" in line.lower():
                principal = line.strip()
                break
        return True, principal or "ticket present"

    def _check_impala_tcp(self) -> tuple[bool, str]:
        """TCP socket connect to Impala — no Impyla dependency."""
        try:
            with socket.create_connection((self.host, self.port), timeout=10):
                pass
        except OSError as exc:
            return False, f"Cannot reach {self.host}:{self.port} — {exc}"
        return True, f"TCP {self.host}:{self.port} reachable"

    def _check_database(self) -> tuple[bool, str]:
        """Open a fresh Impala connection, run SELECT 1 then SHOW DATABASES."""
        try:
            from impala.dbapi import connect  # type: ignore[import]
        except ImportError:
            return False, "impyla not installed — run: pip install impyla thrift_sasl"

        try:
            conn = connect(
                host=self.host,
                port=self.port,
                auth_mechanism="GSSAPI",
                kerberos_service_name=self.kerberos_service_name,
            )
        except Exception as exc:
            return False, f"Connection failed: {exc}"

        try:
            cur = conn.cursor()

            # Apply any configured session settings on this health-check connection
            for stmt in self._session_settings:
                try:
                    cur.execute(stmt)
                    logger.info("[health/session] Applied: %s", stmt)
                except Exception as exc:
                    logger.warning("[health/session] Failed to apply %r: %s", stmt, exc)

            cur.execute("SELECT 1")
            cur.fetchall()
            logger.info("[health] SELECT 1 OK")

            cur.execute("SHOW DATABASES")
            dbs = [row[0] for row in cur.fetchall()]
            cur.close()
            conn.close()
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            return False, f"Database check failed: {exc}"

        sample = ", ".join(dbs[:5]) + (" …" if len(dbs) > 5 else "")
        return True, f"SELECT 1 OK; SHOW DATABASES OK — [{sample}]"

    def _check_iceberg_read(self) -> tuple[bool, str]:
        """
        Validate end-to-end Iceberg table read via Impala.

        Runs:  SELECT * FROM curated_datamodels.citizen_student LIMIT 1

        This is the primary validation that the Iceberg/Parquet stack works
        correctly through Impala (the original motivation for this migration).
        """
        try:
            from impala.dbapi import connect  # type: ignore[import]
        except ImportError:
            return False, "impyla not installed — run: pip install impyla thrift_sasl"

        iceberg_table = f"{self.database}.citizen_student"
        query = f"SELECT * FROM {iceberg_table} LIMIT 1"

        try:
            conn = connect(
                host=self.host,
                port=self.port,
                auth_mechanism="GSSAPI",
                kerberos_service_name=self.kerberos_service_name,
            )
        except Exception as exc:
            return False, f"Connection failed for Iceberg check: {exc}"

        try:
            cur = conn.cursor()
            cur.execute(query)
            rows = cur.fetchall()
            description = cur.description or []
            col_count = len(description)
            cur.close()
            conn.close()
        except Exception as exc:
            try:
                conn.close()
            except Exception:
                pass
            return False, f"Iceberg read failed ({iceberg_table}): {exc}"

        row_info = f"{len(rows)} row(s), {col_count} column(s)"
        return True, f"Iceberg read OK — {iceberg_table}: {row_info}"

    # ── Query execution ───────────────────────────────────────────────────────

    def execute(self, query: str) -> str:
        """
        Validate and execute an Impala SQL query.

        Returns JSON matching the MCP contract:
          success → { status, columns, rows, row_count }
          error   → { status, error_type, error_msg, query }
        """
        logger.info("[execute] %s", query[:200])

        # ── 1. Validate ───────────────────────────────────────────────────────
        ok, reason = _validate_query(query)
        if not ok:
            logger.warning("[execute] blocked: %s", reason)
            return json.dumps({
                "status":     "error",
                "error_type": "validation_error",
                "error_msg":  reason,
                "query":      query,
            })

        # ── 2. Execute with timeout + cursor.cancel() on expiry ───────────────
        result_holder: dict[str, Any] = {}
        error_holder:  dict[str, Any] = {}
        cursor_holder: dict[str, Any] = {}   # shared with timeout handler

        def _run() -> None:
            try:
                with self._lock:
                    conn = self._get_connection()

                cursor = conn.cursor()
                cursor_holder["cursor"] = cursor

                cursor.execute(query)

                description = cursor.description or []
                columns = [col[0] for col in description]
                raw_rows = cursor.fetchall()
                cursor.close()

                # Impyla returns native Python types (datetime.date,
                # datetime.datetime, Decimal, etc.) — no post-fetch casting needed.
                rows = [dict(zip(columns, row)) for row in raw_rows]

                result_holder["columns"]   = columns
                result_holder["rows"]      = rows
                result_holder["row_count"] = len(rows)

            except Exception as exc:
                logger.error("[execute] error: %s", exc, exc_info=True)
                self._reset_connection()
                error_holder["exc"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=self._timeout)

        if thread.is_alive():
            # ── Cancel the server-side query to avoid orphaned Impala jobs ────
            cursor = cursor_holder.get("cursor")
            if cursor is not None:
                try:
                    cursor.cancel()
                    logger.info("[execute] cursor.cancel() sent to Impala")
                except Exception as cancel_exc:
                    logger.warning("[execute] cursor.cancel() failed: %s", cancel_exc)
            self._reset_connection()
            logger.error("[execute] query timed out after %ss", self._timeout)
            return json.dumps({
                "status":     "error",
                "error_type": "timeout_error",
                "error_msg":  (
                    f"Query cancelled after {self._timeout}s timeout. "
                    "The Impala query has been requested to stop."
                ),
                "query": query,
            })

        if error_holder:
            exc = error_holder["exc"]
            return json.dumps({
                "status":     "error",
                "error_type": "execution_error",
                "error_msg":  str(exc),
                "query":      query,
            })

        return json.dumps(
            {
                "status":    "success",
                "columns":   result_holder["columns"],
                "rows":      result_holder["rows"],
                "row_count": result_holder["row_count"],
            },
            default=str,   # serialises date/datetime/Decimal safely
        )

    def close(self):
        self._reset_connection()
        logger.info("HiveExecutor (Impala) connection closed")
