"""
Build Milvus Lite schema index from schema/curated_datamodels/.

Run from project root (Ollama + nomic-embed-text required):
    python MCP/build_milvus_index.py

Regenerate per-table YAMLs from full_schema.yaml:
    python -m schema.split_tables
"""
import os
import sys

import requests
import yaml

MCP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MCP_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from schema.paths import JOIN_RELATIONS_YAML, TABLES_DIR, ensure_table_yaml_files  # noqa: E402

CONFIG_PATH = os.path.join(MCP_DIR, "mcp_rag.yaml")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_milvus_uri(cfg: dict) -> str:
    uri = cfg["vector_db"]["milvus"]["uri"]
    if uri.startswith(("http://", "https://")) or os.path.isabs(uri):
        return uri
    return os.path.normpath(os.path.join(MCP_DIR, uri))


def load_table_chunks(yaml_files: list) -> list[dict]:
    chunks = []
    for path in yaml_files:
        path = str(path)
        with open(path, "r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if not doc:
            continue

        table_name = doc.get("table") or os.path.splitext(os.path.basename(path))[0]
        database_name = doc.get("database", "curated_datamodels")
        raw_ddl = (doc.get("raw_ddl") or "").strip()
        description = doc.get("description", "")

        col_lines = []
        for col in doc.get("columns") or []:
            name = col.get("name", "")
            ctype = col.get("type", "")
            desc = col.get("description", "")
            col_lines.append(f"- {name} ({ctype}): {desc}")

        relationships = doc.get("relationships") or []
        rel_text = "\n".join(f"- {r}" for r in relationships) if relationships else ""

        embedding_text = (
            f"DDL:\n{raw_ddl}\n\n"
            f"Table Description:\n{description}\n\n"
            f"Column Descriptions:\n" + "\n".join(col_lines) + "\n\n"
            f"Relationships:\n{rel_text}"
        ).strip()

        chunks.append({
            "database_name": database_name,
            "table_name": table_name,
            "raw_ddl": raw_ddl,
            "embedding_text": embedding_text,
            "source_file": path,
        })
    return chunks


def load_join_chunks() -> list[dict]:
    if not JOIN_RELATIONS_YAML.is_file():
        return []

    with open(JOIN_RELATIONS_YAML, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    database_name = doc.get("database", "curated_datamodels")
    joins = doc.get("joins") or []
    lines = [doc.get("description", "Join relations for curated school data model.")]
    for join in joins:
        lines.append(
            "{name}: {from_table}.{from_column} -> {to_table}.{to_column} "
            "({cardinality}). Usage: {usage}".format(
                name=join.get("name", "join"),
                from_table=join.get("from_table", ""),
                from_column=join.get("from_column", ""),
                to_table=join.get("to_table", ""),
                to_column=join.get("to_column", ""),
                cardinality=join.get("cardinality", "unknown"),
                usage=join.get("usage", ""),
            )
        )

    embedding_text = "Database: {db}\nChunk: join_relations\n{body}".format(
        db=database_name,
        body="\n".join(lines),
    )
    return [{
        "database_name": database_name,
        "table_name": "__join_relations__",
        "raw_ddl": "",
        "embedding_text": embedding_text,
        "source_file": str(JOIN_RELATIONS_YAML),
    }]


_MAX_EMBED_CHARS = 4096  # ~1024 tokens; keeps Ollama context safe


def embed_text(text: str, cfg: dict) -> list[float]:
    emb_cfg = cfg["embedding"]
    if emb_cfg["provider"] != "ollama":
        raise ValueError("Only ollama embeddings supported for index build")

    url = emb_cfg.get("ollama_url", "http://localhost:11434/api/embeddings")
    model = emb_cfg["model"]
    # Truncate to avoid Ollama 500s on huge DDLs
    prompt = text[:_MAX_EMBED_CHARS]
    res = requests.post(url, json={"model": model, "prompt": prompt}, timeout=120)
    res.raise_for_status()
    return res.json()["embedding"]


def build_index():
    cfg = load_config()
    collection = cfg["vector_db"]["milvus"]["collection"]
    dim = cfg["vector_db"]["milvus"]["dim"]
    uri = resolve_milvus_uri(cfg)

    yaml_files = ensure_table_yaml_files()
    chunks = load_table_chunks(yaml_files) + load_join_chunks()
    if not chunks:
        print(f"No schema chunks to index under {TABLES_DIR}.", file=sys.stderr)
        sys.exit(1)

    import uuid
    from pymilvus import MilvusClient, DataType

    rows = []
    skipped = 0
    for i, chunk in enumerate(chunks):
        try:
            vector = embed_text(chunk["embedding_text"], cfg)
        except Exception as exc:
            print(
                f"  [SKIP] {chunk['table_name']}: embedding failed — {exc}",
                file=sys.stderr,
            )
            skipped += 1
            continue
        rows.append({
            "id": str(uuid.uuid4()),
            "embedding": vector,
            "database_name": chunk["database_name"],
            "table_name": chunk["table_name"],
            "raw_ddl": chunk["raw_ddl"][:65000],
            "embedding_text": chunk["embedding_text"][:9900],
            "source_file": chunk.get("source_file", "unknown"),
        })
    if skipped:
        print(f"  Warning: {skipped} chunk(s) skipped due to embedding errors.", file=sys.stderr)

    client = MilvusClient(uri=uri)
    if client.has_collection(collection):
        client.drop_collection(collection)

    # Replicate pipeline.py collection initialization
    schema = MilvusClient.create_schema(
        auto_id=False,
        enable_dynamic_field=False,
    )
    schema.add_field(
        field_name="id",
        datatype=DataType.VARCHAR,
        is_primary=True,
        max_length=64,
    )
    schema.add_field(
        field_name="database_name",
        datatype=DataType.VARCHAR,
        max_length=256,
    )
    schema.add_field(
        field_name="table_name",
        datatype=DataType.VARCHAR,
        max_length=256,
    )
    schema.add_field(
        field_name="embedding_text",
        datatype=DataType.VARCHAR,
        max_length=10000,
    )
    schema.add_field(
        field_name="raw_ddl",
        datatype=DataType.VARCHAR,
        max_length=65535,
    )
    schema.add_field(
        field_name="source_file",
        datatype=DataType.VARCHAR,
        max_length=512,
    )
    schema.add_field(
        field_name="embedding",
        datatype=DataType.FLOAT_VECTOR,
        dim=dim,
    )

    index_params = MilvusClient.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        index_type="FLAT",
        metric_type=cfg["vector_db"]["milvus"].get("metric_type", "COSINE"),
    )

    client.create_collection(
        collection_name=collection,
        schema=schema,
        index_params=index_params,
    )

    # Idempotently create schema_store and few_shot_store partitions
    PARTITION_SCHEMA = "schema_store"
    PARTITION_FEW_SHOT = "few_shot_store"
    for part in (PARTITION_SCHEMA, PARTITION_FEW_SHOT):
        if not client.has_partition(collection_name=collection, partition_name=part):
            client.create_partition(collection_name=collection, partition_name=part)

    client.insert(collection_name=collection, data=rows, partition_name=PARTITION_SCHEMA)
    client.load_collection(collection)

    print(f"Indexed {len(rows)} schema chunks from {TABLES_DIR} and {JOIN_RELATIONS_YAML}")
    print(f"  → {uri} (collection={collection})")


if __name__ == "__main__":
    build_index()
