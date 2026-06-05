# Curated School Datamodel — NL2SQL Agent

FastAPI + LangGraph backend for querying the `curated_datamodels` school schema via natural language.
Includes a **modular vector-DB injection pipeline** (`pipeline.py`) that populates Milvus with
both table schemas and few-shot NL→SQL exemplars.

---

## Components

| Component | Technology |
|---|---|
| API server | FastAPI (`app.py`) |
| Agent | LangGraph (`my_agent/`) |
| Chat + embedding model | Ollama (`nomic-embed-text` + `qwen3.5`) |
| Schema retrieval | MCP `retrive_schema_rag` → Milvus Lite |
| SQL execution | MCP `execute_sql` → SQLite or Hive |
| **Vector injection** | **`pipeline.py`** — schema + few-shot |

---

## Milvus Collection Layout

One collection (`schema_chunks`) with **two named partitions**:

| Partition | Contents | `embedding_text` | `raw_ddl` |
|---|---|---|---|
| `schema_store` | One doc per table YAML | Schema prose | DDL string |
| `few_shot_store` | NL→SQL exemplars | NL question | Gold SQL |

---

## Setup

```bash
# 1. Create and activate virtualenv
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt

# 2. Build SQLite sample database
python create_schema.py

# 3. Inject table schemas into Milvus  (schema_store partition)
python pipeline.py --config config.yaml

# 4. Inject few-shot NL→SQL pairs     (few_shot_store partition)
python pipeline.py --config config.yaml --fewshots school_dropout_fewshots_200.jsonl

# 5. Start the server
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Deploying to Server

On a fresh server, after installing dependencies and pulling Ollama models, run these **once** to build the vector index before starting the API:

```bash
# First time only — inject schemas + few-shots into Milvus
python pipeline.py --config config.yaml
python pipeline.py --config config.yaml --fewshots school_dropout_fewshots_200.jsonl

# Then start the server
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

> Re-run the pipeline commands any time you add new YAML schemas or few-shot examples.

---

## Hive Mode (Production Server)

Switch from SQLite to HiveServer2 for production queries. One flag controls the entire stack — SQL execution, LLM system prompt, and DDL preprocessing all switch together.

### Complete Hive Setup (run in order on the server)

> ⚠️ Do this **every session** before starting the app. Kerberos tickets expire.

```bash
# 1. Export Hadoop env
export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-1.8.0.492.b09-2.el9.x86_64/jre
export HADOOP_HOME=/usr/local/hadoop-3.3.6
export HADOOP_CONF_DIR=$HOME/hadoop-configuration
export PATH=$HADOOP_HOME/bin:$PATH
export CLASSPATH=$(hadoop classpath --glob)

# 2. Get Kerberos ticket
kinit <your-principal>

# 3. Validate everything (Java, Hadoop, Kerberos, HDFS, HiveServer2 — 7 checks)
python MCP/hive_startup_check.py

# 4. Then flip the flag and start
```

In `.env`, set:
```
HIVE_MCP_ENABLED=true
```

Then start the server (inject vectors first if not already done):
```bash
# First time only
python pipeline.py --config config.yaml
python pipeline.py --config config.yaml --fewshots school_dropout_fewshots_200.jsonl

# Start
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

### What changes when you flip `HIVE_MCP_ENABLED=true`

| | SQLite (`false`) | Hive (`true`) |
|---|---|---|
| SQL execution | `mcp_sql_execution.py` → SQLite | `mcp_hive_execution.py` → HiveServer2 |
| LLM prompt | "SQLite, no prefix" | "Hive, use `curated_datamodels.table`" |
| DDL in RAG | Stripped (Iceberg → SQLite types) | Returned as-is |

### KeyError 22 — Handled Automatically

PyHive cannot process `timestamptz` columns (`created_date`, `updated_date`). The execution layer transparently rewrites SQL and casts affected columns to strings in Python — no action needed from users or the LLM. To add more affected columns, edit `execution.timestamptz_columns` in [`MCP/hive_config.yaml`](MCP/hive_config.yaml).

---

> **Prerequisite**: Ollama must be running and `nomic-embed-text` pulled before steps 3–4:
> ```bash
> ollama pull nomic-embed-text
> ```
> Configure chat model and Hive/SQLite mode in `.env`.

---

## Pipeline Config (`config.yaml`)

```yaml
yaml_dir: ./schema/curated_datamodels/tables   # recursive YAML scan

embedding:
  provider: ollama          # openai | sentence_transformers | ollama
  model: nomic-embed-text   # dim=768; change dim in milvus block if you switch models
  ollama_url: http://localhost:11434/api/embeddings

vector_db:
  provider: milvus
  milvus:
    uri: ./milvus_schemas.db   # Milvus Lite (.db file) — swap to http://host:19530 for server
    collection: schema_chunks
    dim: 768
    metric_type: COSINE
```

Override `yaml_dir` at run time without editing the file:
```bash
python pipeline.py --config config.yaml --yaml_dir ./path/to/other/yamls
```

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check |
| `POST` | `/ask` | Natural language → SQL + result rows |

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "How many students are in the database?"}'
```

Response:
```json
{ "sql": "SELECT COUNT(*) FROM citizen_student", "result": [{"COUNT(*)": 1000}] }
```

---

## Agent Flow

```
START → llm_node ↔ tool_node → END
                 ↑          ↑
         retrive_schema_rag  execute_sql
```

- `retrive_schema_rag` queries Milvus (`schema_store` or both partitions)
- `execute_sql` runs read-only SELECT against SQLite or Hive
- Nudge logic re-prompts smaller models (2b) if they skip tool calls

---

## SQLite vs Hive (`.env`)

| Variable | SQLite mode | Hive mode |
|---|---|---|
| `HIVE_MCP_ENABLED` | `false` | `true` |
| SQL dialect | No `db.table` prefix | `curated_datamodels.table` |
| DDL preprocessing | Strip Iceberg keywords, remap types | Return as-is |
