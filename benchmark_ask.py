#!/usr/bin/env python3
import json
import time
import requests
import sys
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parent
FEWSHOTS_PATH = ROOT_DIR / "school_dropout_fewshots_combined.jsonl"
ASK_URL = "http://localhost:8000/ask"

def main():
    if not FEWSHOTS_PATH.is_file():
        print(f"Error: fewshots file not found at {FEWSHOTS_PATH}")
        sys.exit(1)

    print(f"Loading questions from: {FEWSHOTS_PATH.name}")
    print(f"Sending requests to: {ASK_URL}")
    print("=" * 60)

    try:
        lines = FEWSHOTS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        print(f"Error reading fewshots file: {e}")
        sys.exit(1)

    total_time = 0.0
    count = 0
    success_count = 0

    for idx, line in enumerate(lines, 1):
        if not line.strip():
            continue

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        question = data.get("question")
        difficulty = data.get("difficulty", "unknown")
        qid = data.get("id", f"query_{idx}")

        if not question:
            continue

        count += 1
        start_time = time.time()
        
        try:
            # We use a relatively high timeout since LLM calls can take time
            response = requests.post(ASK_URL, json={"question": question}, timeout=180)
            elapsed = time.time() - start_time
            total_time += elapsed

            if response.status_code == 200:
                print(f"Complexity: {difficulty} | Time taken: {elapsed:.2f}s")
                success_count += 1
            else:
                try:
                    err_msg = response.json().get("detail", response.text)
                except Exception:
                    err_msg = response.text
                print(f"[ERROR] Query {qid} failed (Status: {response.status_code}): {err_msg}")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Query {qid} connection failed: {e}")

    print("=" * 60)
    print("Benchmark Completed:")
    print(f"Total Questions: {count}")
    print(f"Successful     : {success_count}")
    print(f"Failed         : {count - success_count}")
    if success_count > 0:
        print(f"Average Time   : {total_time / success_count:.2f}s")
    print("=" * 60)

if __name__ == "__main__":
    main()
