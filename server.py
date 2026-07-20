"""Open Brain MCP — self-hosted thought memory (Postgres + pgvector + local embeddings)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

import numpy as np
import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg.rows import dict_row

mcp = FastMCP("open-brain")

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is not set (postgresql://user:pass@host:5432/openbrain)")
EMBED_MODEL = os.environ.get("OPEN_BRAIN_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM = 384

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        from fastembed import TextEmbedding

        _embedder = TextEmbedding(model_name=EMBED_MODEL)
    return _embedder


def _embed(text: str) -> list[float]:
    model = _get_embedder()
    vec = list(model.embed([text]))[0]
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tolist()


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in values) + "]"


def _conn():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def _fingerprint(content: str) -> str:
    norm = re.sub(r"\s+", " ", content.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@mcp.tool()
def brain_health() -> dict:
    """Health check: DB connectivity, thought count, embedding model."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM thoughts")
            n = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM thoughts WHERE embedding IS NOT NULL"
            )
            with_emb = cur.fetchone()["n"]
        return {
            "success": True,
            "thoughts": n,
            "with_embeddings": with_emb,
            "embed_model": EMBED_MODEL,
            "embed_dim": EMBED_DIM,
            "database": "openbrain@localhost",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def capture_thought(content: str, metadata_json: str = "{}") -> dict:
    """
    Store a thought in Open Brain (deduped by content fingerprint).
    metadata_json: optional JSON object string, e.g. {"project":"miss-minutes","type":"decision"}
    """
    content = (content or "").strip()
    if not content:
        return {"success": False, "error": "content required"}
    try:
        meta = json.loads(metadata_json) if metadata_json else {}
        if not isinstance(meta, dict):
            return {"success": False, "error": "metadata_json must be a JSON object"}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"invalid metadata_json: {e}"}

    try:
        emb = _embed(content)
        fp = _fingerprint(content)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO thoughts (content, content_fingerprint, metadata, embedding)
                VALUES (%s, %s, %s::jsonb, %s::vector)
                ON CONFLICT (content_fingerprint) WHERE content_fingerprint IS NOT NULL
                DO UPDATE SET
                  updated_at = now(),
                  metadata = thoughts.metadata || EXCLUDED.metadata,
                  embedding = EXCLUDED.embedding
                RETURNING id, content_fingerprint, created_at, updated_at
                """,
                (content, fp, json.dumps(meta), _vec_literal(emb)),
            )
            row = cur.fetchone()
            conn.commit()
        return {
            "success": True,
            "id": str(row["id"]),
            "fingerprint": row["content_fingerprint"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def search_thoughts(
    query: str,
    limit: int = 8,
    threshold: float = 0.35,
    metadata_filter_json: str = "{}",
) -> dict:
    """
    Semantic search over stored thoughts.
    threshold: cosine similarity floor (local bge-small often ~0.3–0.7; default 0.35).
    metadata_filter_json: optional JSON object that must be contained in metadata.
    """
    query = (query or "").strip()
    if not query:
        return {"success": False, "error": "query required"}
    limit = max(1, min(int(limit), 50))
    try:
        filt = json.loads(metadata_filter_json) if metadata_filter_json else {}
        if not isinstance(filt, dict):
            return {"success": False, "error": "metadata_filter_json must be object"}
    except json.JSONDecodeError as e:
        return {"success": False, "error": f"invalid filter: {e}"}

    try:
        emb = _embed(query)
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, metadata, similarity, created_at
                FROM match_thoughts(%s::vector, %s, %s, %s::jsonb)
                """,
                (_vec_literal(emb), float(threshold), limit, json.dumps(filt)),
            )
            rows = cur.fetchall()
        results = [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "metadata": r["metadata"],
                "similarity": float(r["similarity"]) if r["similarity"] is not None else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
        return {"success": True, "count": len(results), "results": results}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def recent_thoughts(limit: int = 10) -> dict:
    """Browse most recent thoughts (no embedding)."""
    limit = max(1, min(int(limit), 50))
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, metadata, created_at, updated_at,
                       (embedding IS NOT NULL) AS has_embedding
                FROM thoughts
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return {
            "success": True,
            "count": len(rows),
            "results": [
                {
                    "id": str(r["id"]),
                    "content": r["content"],
                    "metadata": r["metadata"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "has_embedding": bool(r["has_embedding"]),
                }
                for r in rows
            ],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_thought(thought_id: str) -> dict:
    """Fetch one thought by UUID."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, content, metadata, created_at, updated_at,
                       content_fingerprint, (embedding IS NOT NULL) AS has_embedding
                FROM thoughts WHERE id = %s::uuid
                """,
                (thought_id,),
            )
            r = cur.fetchone()
        if not r:
            return {"success": False, "error": "not found"}
        return {
            "success": True,
            "thought": {
                "id": str(r["id"]),
                "content": r["content"],
                "metadata": r["metadata"],
                "fingerprint": r["content_fingerprint"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                "has_embedding": bool(r["has_embedding"]),
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def brain_stats() -> dict:
    """Aggregate stats for Open Brain."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM thoughts")
            total = cur.fetchone()["n"]
            cur.execute(
                "SELECT count(*) AS n FROM thoughts WHERE embedding IS NOT NULL"
            )
            emb = cur.fetchone()["n"]
            cur.execute(
                "SELECT min(created_at) AS first, max(created_at) AS last FROM thoughts"
            )
            span = cur.fetchone()
        return {
            "success": True,
            "total_thoughts": total,
            "with_embeddings": emb,
            "first_created": span["first"].isoformat() if span["first"] else None,
            "last_created": span["last"].isoformat() if span["last"] else None,
            "embed_model": EMBED_MODEL,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    mcp.run(transport="stdio")
