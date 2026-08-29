"""Table definitions for both dialects.

The two DDL blocks are kept literal rather than generated from a type map. They
diverge in only a handful of places (serial vs autoincrement, jsonb vs text,
timestamptz vs text) and reading them side by side beats reading a generator.

Statements are separated by ";\\n" -- keep that separator intact when editing.
"""

DDL_POSTGRES = """
CREATE TABLE IF NOT EXISTS posts (
  id text PRIMARY KEY,
  subreddit text NOT NULL,
  vertical text NOT NULL,
  title text NOT NULL,
  body text,
  author text,
  score int,
  num_comments int,
  created_utc timestamptz,
  permalink text,
  fetched_at timestamptz,
  stage1_keep boolean,
  stage1_reason text
);
CREATE INDEX IF NOT EXISTS posts_created_idx ON posts (created_utc);
CREATE INDEX IF NOT EXISTS posts_stage1_idx ON posts (stage1_keep);
CREATE TABLE IF NOT EXISTS comments (
  id text PRIMARY KEY,
  post_id text REFERENCES posts(id),
  body text,
  author text,
  score int,
  created_utc timestamptz,
  fetched_at timestamptz
);
CREATE INDEX IF NOT EXISTS comments_post_idx ON comments (post_id);
CREATE TABLE IF NOT EXISTS classifications (
  post_id text PRIMARY KEY REFERENCES posts(id),
  is_painpoint boolean,
  pain_summary text,
  vertical text,
  current_workaround text,
  evidence_quote text,
  competitors jsonb,
  scores jsonb,
  total_score int,
  model text,
  classified_at timestamptz
);
CREATE INDEX IF NOT EXISTS classifications_pain_idx ON classifications (is_painpoint);
CREATE TABLE IF NOT EXISTS embeddings (
  post_id text PRIMARY KEY REFERENCES posts(id),
  backend text NOT NULL,
  dim int NOT NULL,
  vector jsonb NOT NULL,
  embedded_at timestamptz
);
CREATE TABLE IF NOT EXISTS clusters (
  id serial PRIMARY KEY,
  canonical_pain text,
  vertical text,
  member_count int,
  first_seen timestamptz,
  last_seen timestamptz,
  avg_score numeric,
  median_score numeric,
  cluster_score numeric,
  prev_month_count int,
  this_month_count int,
  computed_at timestamptz
);
CREATE INDEX IF NOT EXISTS clusters_vertical_idx ON clusters (vertical);
CREATE TABLE IF NOT EXISTS cluster_members (
  cluster_id int REFERENCES clusters(id) ON DELETE CASCADE,
  post_id text REFERENCES posts(id),
  similarity numeric,
  PRIMARY KEY (cluster_id, post_id)
);
CREATE INDEX IF NOT EXISTS cluster_members_post_idx ON cluster_members (post_id);
"""

DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS posts (
  id text PRIMARY KEY,
  subreddit text NOT NULL,
  vertical text NOT NULL,
  title text NOT NULL,
  body text,
  author text,
  score int,
  num_comments int,
  created_utc text,
  permalink text,
  fetched_at text,
  stage1_keep int,
  stage1_reason text
);
CREATE INDEX IF NOT EXISTS posts_created_idx ON posts (created_utc);
CREATE INDEX IF NOT EXISTS posts_stage1_idx ON posts (stage1_keep);
CREATE TABLE IF NOT EXISTS comments (
  id text PRIMARY KEY,
  post_id text REFERENCES posts(id),
  body text,
  author text,
  score int,
  created_utc text,
  fetched_at text
);
CREATE INDEX IF NOT EXISTS comments_post_idx ON comments (post_id);
CREATE TABLE IF NOT EXISTS classifications (
  post_id text PRIMARY KEY REFERENCES posts(id),
  is_painpoint int,
  pain_summary text,
  vertical text,
  current_workaround text,
  evidence_quote text,
  competitors text,
  scores text,
  total_score int,
  model text,
  classified_at text
);
CREATE INDEX IF NOT EXISTS classifications_pain_idx ON classifications (is_painpoint);
CREATE TABLE IF NOT EXISTS embeddings (
  post_id text PRIMARY KEY REFERENCES posts(id),
  backend text NOT NULL,
  dim int NOT NULL,
  vector text NOT NULL,
  embedded_at text
);
CREATE TABLE IF NOT EXISTS clusters (
  id integer PRIMARY KEY AUTOINCREMENT,
  canonical_pain text,
  vertical text,
  member_count int,
  first_seen text,
  last_seen text,
  avg_score real,
  median_score real,
  cluster_score real,
  prev_month_count int,
  this_month_count int,
  computed_at text
);
CREATE INDEX IF NOT EXISTS clusters_vertical_idx ON clusters (vertical);
CREATE TABLE IF NOT EXISTS cluster_members (
  cluster_id int REFERENCES clusters(id) ON DELETE CASCADE,
  post_id text REFERENCES posts(id),
  similarity real,
  PRIMARY KEY (cluster_id, post_id)
);
CREATE INDEX IF NOT EXISTS cluster_members_post_idx ON cluster_members (post_id);
"""
