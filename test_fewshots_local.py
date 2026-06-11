import json
import sqlite3
import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "database" / "schema.db"
FEWSHOTS_PATH = ROOT_DIR / "school_dropout_fewshots_combined.jsonl"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    lines = FEWSHOTS_PATH.read_text(encoding="utf-8").splitlines()
    total = 0
    passed = 0
    failed = 0

    print("Verifying few-shot SQL queries against local SQLite database...\n")

    for idx, line in enumerate(lines, 1):
        if not line.strip(): continue
        data = json.loads(line)
        sql = data["sql"]
        
        # Strip Hive-specific prefixes for SQLite compatibility
        sqlite_sql = sql.replace("curated_datamodels.", "")
        
        total += 1
        try:
            cursor.execute(sqlite_sql)
            # We don't fetchall since we just want to parse and validate syntax/columns
            passed += 1
        except Exception as e:
            failed += 1
            print(f"FAIL: Query {data['id']}")
            print(f"Error: {e}")
            print(f"SQL: {sqlite_sql}\n")
            
    print(f"--- Verification Summary ---")
    print(f"Total: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")

if __name__ == "__main__":
    main()
