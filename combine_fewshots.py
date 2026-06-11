#!/usr/bin/env python3
import json
import re
from pathlib import Path

# Paths
ROOT_DIR = Path(__file__).resolve().parent
PATH_200 = ROOT_DIR / "school_dropout_fewshots_200.jsonl"
PATH_DIVERSE = ROOT_DIR / "school_dropout_fewshots_diverse.jsonl"
PATH_COMBINED = ROOT_DIR / "school_dropout_fewshots_combined.jsonl"

def normalize_sql(sql: str) -> str:
    """
    Normalizes a SQL query to identify its structural template by removing
    variable literals such as specific district names, grades, academic years,
    numeric thresholds, and extra whitespaces.
    """
    sql = sql.lower()
    
    # Normalize academic years (e.g. '2024-25', '2025-26')
    sql = re.sub(r"'\d{4}-\d{2}'", "'ACADEMIC_YEAR'", sql)
    # Normalize numeric strings (e.g. grades '6', '10')
    sql = re.sub(r"'\d+'", "'NUMERIC_LITERAL'", sql)
    # Normalize unquoted numbers/constants
    sql = re.sub(r"\b\d+\b", "NUMERIC_CONSTANT", sql)
    # Normalize string literals (e.g. district names like 'Anantapur')
    sql = re.sub(r"'[^']+'", "'STRING_LITERAL'", sql)
    
    # Normalize multiple spaces, tabs, and newlines
    sql = re.sub(r"\s+", " ", sql)
    return sql.strip()

def main():
    print("--- Starting Few-Shot Merging & Deduplication ---")
    
    if not PATH_200.is_file():
        print(f"Error: {PATH_200} does not exist.")
        return

    if not PATH_DIVERSE.is_file():
        print(f"Error: {PATH_DIVERSE} does not exist.")
        return

    # 1. Load school_dropout_fewshots_200.jsonl
    records_200 = []
    with open(PATH_200, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records_200.append(json.loads(line))
    print(f"Loaded {len(records_200)} records from {PATH_200.name}")

    # 2. Deduplicate 200 records based on SQL template structure
    seen_templates = set()
    deduped_200 = []
    
    for record in records_200:
        sql = record.get("sql", "")
        norm_sql = normalize_sql(sql)
        if norm_sql not in seen_templates:
            seen_templates.add(norm_sql)
            deduped_200.append(record)
            
    print(f"Deduplicated to {len(deduped_200)} unique SQL structures.")

    # 3. Load school_dropout_fewshots_diverse.jsonl
    records_diverse = []
    with open(PATH_DIVERSE, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records_diverse.append(json.loads(line))
    print(f"Loaded {len(records_diverse)} records from {PATH_DIVERSE.name}")

    # 4. Merge records
    combined_records = deduped_200 + records_diverse
    print(f"Merged total records count: {len(combined_records)}")

    # 5. Check integrity (unique IDs and fields presence)
    seen_ids = set()
    errors = 0
    required_keys = {"id", "use_case", "intent", "topic", "difficulty", "dialect", "question", "sql", "tables", "risk_signal", "grain", "output_columns"}
    
    for idx, r in enumerate(combined_records):
        rid = r.get("id")
        if not rid:
            print(f"Error at index {idx}: Missing ID")
            errors += 1
        elif rid in seen_ids:
            print(f"Error: Duplicate ID found: {rid}")
            errors += 1
        else:
            seen_ids.add(rid)
            
        missing_keys = required_keys - set(r.keys())
        if missing_keys:
            print(f"Warning for ID {rid}: Missing keys {missing_keys}")

    if errors > 0:
        print(f"Aborting due to {errors} errors found in combined records.")
        return

    # 6. Save combined records
    with open(PATH_COMBINED, "w", encoding="utf-8") as f:
        for r in combined_records:
            f.write(json.dumps(r) + "\n")
            
    print(f"Successfully saved combined few-shots to: {PATH_COMBINED.name}")
    print(f"File size: {PATH_COMBINED.stat().st_size} bytes")
    print("-------------------------------------------------")

if __name__ == "__main__":
    main()
