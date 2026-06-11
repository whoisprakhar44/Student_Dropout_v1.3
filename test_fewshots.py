#!/usr/bin/env python3
"""
Verify all few-shot SQL queries against the production Impala database.
"""

import json
import sys
from pathlib import Path
import yaml

# Paths
ROOT_DIR = Path(__file__).resolve().parent
FEWSHOTS_PATH = ROOT_DIR / "school_dropout_fewshots_combined.jsonl"
MCP_DIR = ROOT_DIR / "MCP"

def main():
    if not FEWSHOTS_PATH.is_file():
        print(f"Error: fewshots file not found at {FEWSHOTS_PATH}")
        sys.exit(1)

    config_path = MCP_DIR / "hive_config.yaml"
    if not config_path.is_file():
        print(f"Error: hive_config.yaml not found at {config_path}")
        sys.exit(1)

    # Read config
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    query_engine = cfg.get("query_engine", {})
    host = query_engine.get("host")
    port = int(query_engine.get("port", 21050))
    kerberos_service_name = query_engine.get("kerberos", {}).get("service_name", "impala")

    print(f"Loading few-shots from: {FEWSHOTS_PATH}")
    lines = FEWSHOTS_PATH.read_text(encoding="utf-8").splitlines()

    total = 0
    passed = 0
    failed = 0

    print(f"Connecting to Impala at {host}:{port} using GSSAPI (service={kerberos_service_name})...")
    try:
        from impala.dbapi import connect
        conn = connect(
            host=host,
            port=port,
            auth_mechanism="GSSAPI",
            kerberos_service_name=kerberos_service_name
        )
    except Exception as e:
        print(f"Error connecting to Impala: {e}")
        print("Make sure you are running inside the virtualenv and have valid Kerberos tickets (kinit).")
        sys.exit(1)

    cursor = conn.cursor()

    # Set execution time limit and join distribution mode to prevent queries from hanging
    try:
        cursor.execute("SET EXEC_TIME_LIMIT_S=30")
    except Exception as e:
        print(f"Warning setting EXEC_TIME_LIMIT_S: {e}")
    try:
        cursor.execute("SET DEFAULT_JOIN_DISTRIBUTION_MODE='SHUFFLE'")
    except Exception as e:
        print(f"Warning setting DEFAULT_JOIN_DISTRIBUTION_MODE: {e}")
    try:
        cursor.execute("SET MEM_LIMIT=2g")
    except Exception as e:
        print(f"Warning setting MEM_LIMIT: {e}")

    for idx, line in enumerate(lines, 1):
        if not line.strip():
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"Line {idx}: JSON Decode Error: {e}")
            continue

        qid = data.get("id")
        sql = data.get("sql")

        if not qid or not sql:
            print(f"Line {idx}: Missing 'id' or 'sql'")
            continue

        total += 1

        # Execute query verbatim
        try:
            print(f"[{total:03d}/{len(lines):03d}] Running {qid}...", end="", flush=True)
            cursor.execute(sql)
            cursor.fetchall()
            passed += 1
            print(" OK")
        except Exception as e:
            failed += 1
            print(" FAIL")
            print(f"Question: {data.get('question')}")
            print(f"Error: {e}")
            print("-" * 60)

    cursor.close()
    conn.close()

    print("\n" + "=" * 40)
    print("Verification Summary:")
    print(f"Total SQL Queries Checked : {total}")
    print(f"Passed                    : {passed}")
    print(f"Failed                    : {failed}")
    print("=" * 40)

    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
