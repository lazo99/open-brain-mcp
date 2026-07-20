# open-brain-mcp

A self-hosted "second brain" MCP server — capture thoughts from any MCP client, search them semantically, keep everything on hardware you control.

Inspired by [Nate B. Jones's Open Brain (OB1)](https://github.com/NateBJones-Projects/OB1), rebuilt from scratch with zero cloud dependencies: local Postgres + pgvector for storage, local [fastembed](https://github.com/qdrant/fastembed) embeddings (BAAI/bge-small-en-v1.5, 384-dim) instead of hosted embedding APIs. No Supabase, no OpenRouter, no data leaving your machine.

## Tools

| Tool | What it does |
|---|---|
| `capture_thought` | Store a thought (deduped by content fingerprint), optional JSON metadata |
| `search_thoughts` | Semantic search with cosine-similarity threshold and metadata filters |
| `recent_thoughts` | Latest N thoughts |
| `get_thought` | Fetch one by id |
| `brain_stats` | Counts, metadata breakdown |
| `brain_health` | DB connectivity + embedding model check |

## Setup

Requires Python 3.11+, Postgres 15+ with the [pgvector](https://github.com/pgvector/pgvector) extension.

```bash
createdb openbrain
psql -d openbrain -c "CREATE EXTENSION vector;"
```

Create a `thoughts` table with a `vector(384)` embedding column and a `match_thoughts` similarity function (see OB1's schema for reference; adjust the dimension to your embedding model).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Set `DATABASE_URL` (the server refuses to start without it):

```bash
export DATABASE_URL="postgresql://user:password@127.0.0.1:5432/openbrain"
```

## Run

**stdio (Claude Code, OpenCode, any local MCP client):**

```bash
./run.sh
```

`run.sh` sources `~/.secrets/open-brain.env` if present, so credentials can stay out of your shell profile.

```bash
claude mcp add open-brain -- /path/to/open-brain-mcp/run.sh
```

**Remote / claude.ai connector (`server_http.py`):** a minimal streamable-HTTP instance exposing only `capture_thought` + `search_thoughts`, meant to sit behind a reverse proxy or Cloudflare Tunnel. It binds to `127.0.0.1:8090` and mounts at a secret path taken from `OPEN_BRAIN_REMOTE_SECRET` — the URL itself is the credential, since claude.ai custom connectors don't support custom auth headers. Run it as a dedicated low-privilege user with a DB role restricted to the `thoughts` table.

## Remote access from another machine (stdio over SSH)

Postgres and the server bind to localhost only. To use the brain from a second machine, bridge stdio over SSH — for example with GCP IAP:

```bash
#!/usr/bin/env bash
exec gcloud compute ssh YOUR_VM --tunnel-through-iap \
  --command='~/Code/open-brain-mcp/run.sh' -- -q -o LogLevel=QUIET
```

Register that wrapper script as the MCP command and the client speaks to the remote brain as if it were local.

## License

MIT
