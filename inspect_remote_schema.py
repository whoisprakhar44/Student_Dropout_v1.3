import os
import sys
import json
from pathlib import Path

# Add MCP folder to path
mcp_path = Path(__file__).resolve().parent / "MCP"
sys.path.insert(0, str(mcp_path))

from hive_executor import HiveExecutor

def main():
    config_path = mcp_path / "hive_config.yaml"
    print(f"Loading config from: {config_path}")
    
    os.environ["HIVE_MCP_ENABLED"] = "true"
    
    try:
        executor = HiveExecutor(str(config_path))
        print("Connected to Impala.")
        
        # Select one row from scheme_benefits_fact
        print("\n--- Selecting 1 row from curated_datamodels.scheme_benefits_fact ---")
        res1 = executor.execute("SELECT * FROM curated_datamodels.scheme_benefits_fact LIMIT 1;")
        payload1 = json.loads(res1)
        if payload1.get("status") == "success":
            rows = payload1.get("rows", [])
            if rows:
                print("Columns:")
                print(list(rows[0].keys()))
                print("Sample Row:")
                print(rows[0])
            else:
                print("No rows found.")
        else:
            print("Error querying scheme_benefits_fact:", payload1.get("error_msg"))

    except Exception as e:
        print("Error during execution:", e)

if __name__ == "__main__":
    main()
