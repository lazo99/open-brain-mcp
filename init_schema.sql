CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS thoughts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  content TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}',
  content_fingerprint TEXT,
  embedding vector(384),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS thoughts_fingerprint_uidx ON thoughts (content_fingerprint)
  WHERE content_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS thoughts_embedding_ivfflat ON thoughts
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

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
