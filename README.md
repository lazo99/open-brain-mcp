# open-brain-mcp

A self-hosted **second brain** MCP server — capture thoughts from any MCP client, search them semantically, keep everything on hardware you control.

Inspired by [Nate B. Jones's Open Brain (OB1)](https://github.com/NateBJones-Projects/OB1), rebuilt with **zero required cloud AI dependencies**: local Postgres + pgvector, local [fastembed](https://github.com/qdrant/fastembed) embeddings (`BAAI/bge-small-en-v1.5`, 384-dim). No Supabase, no hosted embedding API required for core operation.

> **Project state (2026-07-20): usable alpha / production-personal.**  
> Running in production on a single VM for [Miss Minutes](https://mm.ofone.dev) (Hermes agent) with optional **claude.ai** remote connector. API surface is stable enough for daily use; expect small breaking changes until 1.0. Not a multi-tenant SaaS.

## Current status

| Area | State |
|------|--------|
| stdio MCP (local agents) | **Stable** — full tool set |
| HTTP MCP for claude.ai | **Stable** — `capture_thought` + `search_thoughts` only |
| Embeddings | Local CPU via fastembed (first run downloads model) |
| Multi-user auth | **None** — protect HTTP with secret URL path + network controls |
| HA / replication | **Not built** — single Postgres |
| Obsidian sync | **Out of band** — vault is a client/ingest source, not embedded |
| Schema migrations | Manual SQL (see below) |

**Architecture in one line:** one Postgres database on your server; every agent (Hermes, Claude Code, claude.ai, OpenCode, …) is just an MCP client.

```
Claude.ai ──HTTP MCP──┐
Hermes/Discord ─stdio─┼──► open-brain-mcp ──► Postgres+pgvector (you host)
Laptop agent ──stdio/TS┘
```

## Tools

| Tool | stdio | HTTP (claude.ai) | Purpose |
|------|:-----:|:----------------:|---------|
| `capture_thought` | ✅ | ✅ | Store thought (fingerprint dedupe) + optional JSON metadata |
| `search_thoughts` | ✅ | ✅ | Semantic search + threshold + metadata filter |
| `recent_thoughts` | ✅ | — | Latest N |
| `get_thought` | ✅ | — | Fetch by id |
| `brain_stats` | ✅ | — | Counts / breakdown |
| `brain_health` | ✅ | — | DB + embed model check |

## Requirements

- Python 3.11+
- Postgres 15+ with [pgvector](https://github.com/pgvector/pgvector)
- ~500MB+ RAM for embedding model once loaded

## Database

```bash
sudo -u postgres psql -c "CREATE USER openbrain WITH PASSWORD '…';"
sudo -u postgres psql -c "CREATE DATABASE openbrain OWNER openbrain;"
sudo -u postgres psql -d openbrain -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Minimal schema (384-dim):

```sql
CREATE TABLE IF NOT EXISTS thoughts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}',
  fingerprint TEXT,
  embedding vector(384),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS thoughts_fingerprint_uidx ON thoughts (fingerprint)
  WHERE fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS thoughts_embedding_ivfflat ON thoughts
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- similarity helper used by search (cosine distance)
CREATE OR REPLACE FUNCTION match_thoughts(
  query_embedding vector(384),
  match_threshold float DEFAULT 0.35,
  match_count int DEFAULT 8,
  metadata_filter jsonb DEFAULT '{}'::jsonb
) RETURNS TABLE (
  id uuid,
  content text,
  metadata jsonb,
  created_at timestamptz,
  similarity float
) LANGUAGE sql STABLE AS $$
  SELECT t.id, t.content, t.metadata, t.created_at,
         1 - (t.embedding <=> query_embedding) AS similarity
  FROM thoughts t
  WHERE t.embedding IS NOT NULL
    AND 1 - (t.embedding <=> query_embedding) >= match_threshold
    AND (metadata_filter = '{}'::jsonb OR t.metadata @> metadata_filter)
  ORDER BY t.embedding <=> query_embedding
  LIMIT match_count;
$$;
```

> If your live DB already has a compatible `match_thoughts` / `thoughts` schema from an earlier OB1-inspired setup, keep it — adjust only if dimensions differ.

## Install

```bash
git clone https://github.com/lazo99/open-brain-mcp.git
cd open-brain-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Credentials (never commit):

```bash
# ~/.secrets/open-brain.env
DATABASE_URL=postgresql://openbrain:***@127.0.0.1:5432/openbrain
OPEN_BRAIN_EMBED_MODEL=BAAI/bge-small-en-v1.5
# HTTP / claude.ai only:
OPEN_BRAIN_REMOTE_SECRET=long-random-string
# optional convenience:
# OPEN_BRAIN_REMOTE_URL=https://brain.example.com/<secret>/mcp
```

## Run

### stdio (Hermes, Claude Code, OpenCode, Cursor, …)

```bash
./run.sh
# or:
claude mcp add open-brain -- /path/to/open-brain-mcp/run.sh
```

`run.sh` sources `~/.secrets/open-brain.env` when present.

### HTTP for claude.ai (`server_http.py`)

Minimal streamable-HTTP surface (`capture_thought` + `search_thoughts` only).

- Listens on `127.0.0.1:8090`
- MCP path: `/{OPEN_BRAIN_REMOTE_SECRET}/mcp`
- Put a reverse proxy or Cloudflare Tunnel in front (e.g. `brain.example.com`)
- **The URL path is the credential** (claude.ai custom connectors typically cannot set arbitrary auth headers)

```bash
export DATABASE_URL=…
export OPEN_BRAIN_REMOTE_SECRET=…
.venv/bin/python server_http.py
```

In claude.ai: **Settings → Connectors → add remote MCP** → paste:

```
https://brain.example.com/<OPEN_BRAIN_REMOTE_SECRET>/mcp
```

### Remote stdio (second machine)

Keep Postgres bound to localhost. Bridge with SSH/Tailscale:

```bash
#!/usr/bin/env bash
exec ssh user@your-vm '~/Code/open-brain-mcp/run.sh'
```

Register that script as the MCP command on the laptop.

## Production notes (reference deployment)

Personal production pattern used by the author:

- Single GCP VM, Postgres local, Tailscale for admin
- `open-brain-web.service` runs HTTP MCP as a locked-down user
- Cloudflare Tunnel hostname → `127.0.0.1:8090`
- Hermes Agent on the same VM uses **stdio** MCP for full tools
- claude.ai uses **HTTP** MCP for capture/search only
- Secrets: env files + password manager + optional cloud secret manager — not git

## Security

- Treat HTTP secret URLs like passwords; rotate if leaked
- Prefer localhost + tunnel over public bind
- DB user should only need rights on `thoughts` (+ sequence/functions used)
- Do not log request URLs that contain the secret path

## Roadmap (honest)

- [ ] Packaged SQL migration files in-repo
- [ ] Optional token header auth if/when claude.ai supports it cleanly
- [ ] Obsidian plugin or documented ingest recipe
- [ ] Metrics / backup docs
- [ ] 1.0 when schema + HTTP auth story freeze

## License

MIT

## Related

- Upstream idea: [OB1](https://github.com/NateBJones-Projects/OB1)
- This repo: https://github.com/lazo99/open-brain-mcp
