"""
hive_startup_check.py
─────────────────────
Pre-flight validator for the Hive runtime environment.

Run standalone on the server before starting the app:
    python MCP/hive_startup_check.py

Or import and call from app startup:
    from MCP.hive_startup_check import run_all_checks
    run_all_checks()   # raises RuntimeError on first failure

Checks performed (in order):
  1. JAVA_HOME   — env var set and directory exists
  2. HADOOP_HOME — env var set, directory exists, hadoop binary present
  3. HADOOP_CONF_DIR — contains core-site.xml and hdfs-site.xml
  4. Kerberos ticket — `klist` exits 0
  5. HDFS reachable  — `hdfs dfs -ls /` exits 0
  6. HiveServer2 TCP — socket connect to host:port
  7. Hive query      — SHOW DATABASES succeeds via PyHive

Checks 1-6 use only stdlib (no PyHive import needed).
Check 7 requires PyHive and a valid Kerberos ticket.
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import yaml


# ─────────────────────────────────────────────────────────────────────────────
#  Config loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_hive_config() -> dict:
    cfg_path = Path(__file__).parent / "hive_config.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"hive_config.yaml not found at {cfg_path}")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────────
#  Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def check_java_home() -> str:
    """Check 1: JAVA_HOME is set and is a valid directory."""
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if not java_home:
        raise RuntimeError(
            "JAVA_HOME is not set.\n"
            "Fix: export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-.../jre"
        )
    if not Path(java_home).is_dir():
        raise RuntimeError(
            f"JAVA_HOME={java_home!r} does not exist or is not a directory."
        )
    return java_home


def check_hadoop_home() -> str:
    """Check 2: HADOOP_HOME is set, directory exists, hadoop binary present."""
    hadoop_home = os.environ.get("HADOOP_HOME", "").strip()
    if not hadoop_home:
        raise RuntimeError(
            "HADOOP_HOME is not set.\n"
            "Fix: export HADOOP_HOME=/usr/local/hadoop-3.3.6"
        )
    hadoop_path = Path(hadoop_home)
    if not hadoop_path.is_dir():
        raise RuntimeError(f"HADOOP_HOME={hadoop_home!r} does not exist.")

    hadoop_bin = hadoop_path / "bin" / "hadoop"
    if not hadoop_bin.exists():
        raise RuntimeError(f"hadoop binary not found at {hadoop_bin}")
    return hadoop_home


def check_hadoop_conf_dir() -> str:
    """Check 3: HADOOP_CONF_DIR exists and has required XML files."""
    conf_dir = os.environ.get("HADOOP_CONF_DIR", "").strip()
    if not conf_dir:
        raise RuntimeError(
            "HADOOP_CONF_DIR is not set.\n"
            "Fix: export HADOOP_CONF_DIR=$HOME/hadoop-configuration"
        )
    conf_path = Path(conf_dir).expanduser()
    if not conf_path.is_dir():
        raise RuntimeError(
            f"HADOOP_CONF_DIR={conf_dir!r} does not exist or is not a directory."
        )
    for required in ("core-site.xml", "hdfs-site.xml"):
        if not (conf_path / required).exists():
            raise RuntimeError(
                f"Required file missing: {conf_path / required}\n"
                f"HADOOP_CONF_DIR must contain core-site.xml and hdfs-site.xml."
            )
    return str(conf_path)


def check_kerberos_ticket() -> str:
    """Check 4: Valid Kerberos ticket exists (klist exits 0)."""
    klist = shutil.which("klist")
    if not klist:
        raise RuntimeError(
            "klist not found on PATH. Is krb5-workstation / krb5-user installed?"
        )
    result = subprocess.run(
        [klist],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "No valid Kerberos ticket found.\n"
            f"klist output:\n{result.stdout}\n{result.stderr}\n"
            "Fix: kinit <your-principal>"
        )
    # Extract principal from klist output for display
    principal = ""
    for line in result.stdout.splitlines():
        if "principal" in line.lower():
            principal = line.strip()
            break
    return principal or "ticket present"


def check_hdfs(hadoop_home: str) -> str:
    """Check 5: HDFS is reachable (hdfs dfs -ls / exits 0)."""
    hdfs_bin = Path(hadoop_home) / "bin" / "hdfs"
    result = subprocess.run(
        [str(hdfs_bin), "dfs", "-ls", "/"],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"HDFS not reachable.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return "hdfs dfs -ls / OK"


def check_hiveserver2_tcp(host: str, port: int) -> str:
    """Check 6: HiveServer2 TCP port is open (socket connect)."""
    try:
        with socket.create_connection((host, port), timeout=10):
            pass
    except OSError as exc:
        raise RuntimeError(
            f"Cannot connect to HiveServer2 at {host}:{port}\n"
            f"Error: {exc}\n"
            "Verify the host/port and network access."
        ) from exc
    return f"TCP {host}:{port} reachable"


def check_hive_query(host: str, port: int, auth: str, kerberos_service_name: str) -> str:
    """Check 7: Execute SHOW DATABASES via PyHive with Kerberos."""
    try:
        from pyhive import hive
    except ImportError as exc:
        raise RuntimeError(
            "PyHive not installed. Run: pip install 'pyhive[hive]' thrift-sasl"
        ) from exc

    try:
        conn = hive.Connection(
            host=host,
            port=port,
            auth=auth,
            kerberos_service_name=kerberos_service_name,
        )
        cursor = conn.cursor()
        cursor.execute("SHOW DATABASES")
        dbs = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
    except Exception as exc:
        raise RuntimeError(
            f"Hive query failed: {exc}\n"
            "Check Kerberos ticket, network access, and HiveServer2 logs."
        ) from exc

    return f"SHOW DATABASES OK — found: {', '.join(dbs[:5])}"


# ─────────────────────────────────────────────────────────────────────────────
#  Master runner
# ─────────────────────────────────────────────────────────────────────────────

def run_all_checks(raise_on_failure: bool = True) -> bool:
    """
    Run all 7 startup checks in order.

    Args:
        raise_on_failure: If True (default), raise RuntimeError on first
                          failure. If False, print all results and return
                          False if any check failed (useful for diagnostics).

    Returns:
        True if all checks passed, False otherwise.
    """
    cfg      = _load_hive_config()
    hive_cfg = cfg["hive"]
    host     = hive_cfg["host"]
    port     = int(hive_cfg["port"])
    auth     = hive_cfg["auth"]
    ks_name  = hive_cfg.get("kerberos_service_name", "hive")

    checks = [
        ("JAVA_HOME",         lambda: check_java_home()),
        ("HADOOP_HOME",       lambda: check_hadoop_home()),
        ("HADOOP_CONF_DIR",   lambda: check_hadoop_conf_dir()),
        ("Kerberos ticket",   lambda: check_kerberos_ticket()),
        ("HDFS connectivity", lambda: check_hdfs(os.environ.get("HADOOP_HOME", ""))),
        ("HiveServer2 TCP",   lambda: check_hiveserver2_tcp(host, port)),
        ("Hive query",        lambda: check_hive_query(host, port, auth, ks_name)),
    ]

    all_passed = True
    hadoop_home = None

    print("=" * 60)
    print("  Hive Runtime Pre-flight Checks")
    print("=" * 60)

    for name, fn in checks:
        # Check 5 needs hadoop_home from check 2 result — patch it
        try:
            detail = fn()
            print(f"  ✓  {name:<22}  {detail}")
            if name == "HADOOP_HOME":
                hadoop_home = detail
        except RuntimeError as exc:
            print(f"  ✗  {name:<22}  FAILED")
            print(f"         {exc}")
            all_passed = False
            if raise_on_failure:
                print("=" * 60)
                raise
        except Exception as exc:
            print(f"  ✗  {name:<22}  ERROR: {exc}")
            all_passed = False
            if raise_on_failure:
                print("=" * 60)
                raise RuntimeError(f"Unexpected error in check '{name}': {exc}") from exc

    print("=" * 60)
    if all_passed:
        print("  ALL CHECKS PASSED — Hive execution layer ready")
    else:
        print("  SOME CHECKS FAILED — fix the above issues before starting")
    print("=" * 60)

    return all_passed


# ─────────────────────────────────────────────────────────────────────────────
#  CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ok = run_all_checks(raise_on_failure=False)
    sys.exit(0 if ok else 1)
