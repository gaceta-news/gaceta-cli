-- gaceta schema v1. Additive migrations only. privacy: 'public' < 'household' < 'private'.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS readers (
  id TEXT PRIMARY KEY,               -- slug, e.g. 'alice'
  name TEXT NOT NULL,
  langs TEXT NOT NULL DEFAULT 'en',  -- comma list, e.g. 'es,en'
  created_at INTEGER NOT NULL
);

-- Where content comes from. Shared across readers (a channel is a channel).
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,               -- '<kind>:<native id>', e.g. 'youtube:UCbRP3c…', 'feed:hn'
  kind TEXT NOT NULL,                -- youtube | nostr | rss | hn | lab | release | other
  name TEXT,
  url TEXT,
  content_public INTEGER NOT NULL DEFAULT 1   -- 1 = content is published publicly by its author
);

-- One thing that was published. Source content only; nothing about any reader.
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,               -- adapter-stable id (upstream id reused, e.g. 'yt:FAKEyt00001')
  source_id TEXT REFERENCES sources(id),
  author TEXT,
  title TEXT,
  url TEXT,
  published INTEGER,                 -- unix seconds
  body TEXT,                         -- source text/excerpt
  ingested_at INTEGER NOT NULL,
  origin TEXT NOT NULL,               -- adapter name, e.g. 'sqlite'
  author_pubkey TEXT                  -- v10: nostr author hex pubkey (nostr adapters only)
);
CREATE INDEX IF NOT EXISTS ix_items_published ON items(published);

-- What a reader's agent made of an item. Per reader. Notes are about the reader → always private.
CREATE TABLE IF NOT EXISTS annotations (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  item_id TEXT NOT NULL REFERENCES items(id),
  verdict TEXT,                      -- adapter vocabulary normalised: told | noted | skip | pending
  note TEXT,
  library_path TEXT,                 -- optional pointer to a longer note
  item_privacy TEXT NOT NULL DEFAULT 'private' CHECK (item_privacy IN ('public','household','private')),
  privacy_reason TEXT NOT NULL,      -- why this label (audit), e.g. 'default', 'pinned:term#3', 'public-source'
  annotated_at INTEGER,
  PRIMARY KEY (reader_id, item_id)
);

CREATE TABLE IF NOT EXISTS stories (
  id TEXT PRIMARY KEY,
  reader_id TEXT NOT NULL REFERENCES readers(id),
  title TEXT NOT NULL,
  summary TEXT,
  why_it_matters TEXT,               -- reader-specific → story privacy can never be below its items'
  status TEXT NOT NULL DEFAULT 'developing' CHECK (status IN ('developing','settled','corrected')),
  confidence REAL,
  section TEXT,
  privacy TEXT NOT NULL DEFAULT 'private' CHECK (privacy IN ('public','household','private')),
  first_seen INTEGER, last_seen INTEGER, updated_at INTEGER
);

-- Every rewrite keeps the old text (corrections need it; rollback uses it).
CREATE TABLE IF NOT EXISTS story_versions (
  story_id TEXT NOT NULL REFERENCES stories(id),
  version INTEGER NOT NULL,
  title TEXT, summary TEXT, why_it_matters TEXT, confidence REAL,
  model TEXT, written_at INTEGER NOT NULL,
  members INTEGER,                      -- v4: member count when written (incremental write-ups)
  PRIMARY KEY (story_id, version)
);

CREATE TABLE IF NOT EXISTS story_items (
  story_id TEXT NOT NULL REFERENCES stories(id),
  item_id TEXT NOT NULL REFERENCES items(id),
  role TEXT NOT NULL DEFAULT 'update' CHECK (role IN ('origin','update','dissent','correction')),
  PRIMARY KEY (story_id, item_id)
);

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY,
  story_id TEXT NOT NULL REFERENCES stories(id),
  said TEXT NOT NULL, now TEXT NOT NULL, evidence_item_id TEXT REFERENCES items(id),
  at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS editions (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  date TEXT NOT NULL,                -- YYYY-MM-DD
  max_privacy TEXT NOT NULL CHECK (max_privacy IN ('public','household','private')),
  path TEXT NOT NULL,                -- immutable JSON snapshot
  built_at INTEGER NOT NULL,
  PRIMARY KEY (reader_id, date, max_privacy)
);

CREATE TABLE IF NOT EXISTS reactions (
  id INTEGER PRIMARY KEY,
  reader_id TEXT NOT NULL REFERENCES readers(id),
  story_id TEXT REFERENCES stories(id), item_id TEXT REFERENCES items(id),
  kind TEXT NOT NULL,                -- up | down | more | less | wrong | why
  text TEXT, at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS told (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  item_id TEXT REFERENCES items(id), story_id TEXT REFERENCES stories(id),
  channel TEXT, at INTEGER
);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY, adapter TEXT NOT NULL, reader_id TEXT,
  started_at INTEGER NOT NULL, finished_at INTEGER,
  items_new INTEGER DEFAULT 0, items_updated INTEGER DEFAULT 0, error TEXT
);

-- v2 (additive): local embeddings of item source content. One row per (item, model);
-- text_hash makes re-embedding incremental (skip when the source text is unchanged).
-- The embedded text is title + body ONLY — never the annotation note (notes are about
-- the reader; embeddings of shared items must not carry one reader's notes).
CREATE TABLE IF NOT EXISTS embeddings (
  item_id TEXT NOT NULL REFERENCES items(id),
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vec BLOB NOT NULL,
  text_hash TEXT NOT NULL,
  PRIMARY KEY (item_id, model)
);

-- v3 (additive): LLM decision cache. key = sha256(model + prompt), so a rerun of
-- the same decision costs zero calls. output is the raw model reply; usage is JSON
-- (tokens + the model the API actually served). No prompts or keys are stored.
CREATE TABLE IF NOT EXISTS llm_decisions (
  key TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  input_hash TEXT,
  output TEXT,
  usage TEXT,
  at INTEGER NOT NULL
);

-- v5 (additive): media per item (thumbnails, og:image, nostr images, video ids), cached locally.
CREATE TABLE IF NOT EXISTS media (
  item_id TEXT NOT NULL REFERENCES items(id),
  kind TEXT NOT NULL CHECK (kind IN ('image','youtube','video')),
  src_url TEXT,                      -- remote origin (https only)
  local_path TEXT,                   -- relative to <data>/site/media/, NULL until downloaded
  video_id TEXT,                     -- youtube id when kind='youtube'
  width INTEGER, height INTEGER,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','ok','error','none')),
  error TEXT, fetched_at INTEGER,
  PRIMARY KEY (item_id, kind)
);

-- v6 (additive): per-person seen state. A story marked seen at rev R stays
-- hidden for that person until its rev changes (a source joined or the
-- write-up changed). Same shared paper, private reading progress.
CREATE TABLE IF NOT EXISTS seen (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  by_name TEXT NOT NULL,                -- who read it, e.g. 'alice', 'bob'
  story_id TEXT NOT NULL REFERENCES stories(id),
  rev TEXT NOT NULL,                    -- story rev when marked (<=16 hex chars)
  at INTEGER NOT NULL,
  PRIMARY KEY (reader_id, by_name, story_id)
);

-- v7 (additive): the reader's verdict on a discovered source (roadmap 6b,
-- DECISIONS 11/13). gaceta never edits a collector: the reader's agent reads
-- these rows via `gaceta sources pending` and applies them upstream.
CREATE TABLE IF NOT EXISTS source_decisions (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  by_name TEXT NOT NULL,                -- who decided; only the owner's land
  source_key TEXT NOT NULL,             -- discovery key: 'feed:trial-x', 'nostr:<hex>', 'host:<host>'
  decision TEXT NOT NULL CHECK (decision IN ('follow','drop','later')),
  at INTEGER NOT NULL,
  exported_at INTEGER,                  -- set by `gaceta sources pending --mark-exported`
  PRIMARY KEY (reader_id, source_key)
);

-- v8 (additive): stories the reader handed to their own agent ("send" button,
-- roadmap 6c). gaceta only records and hands off: delivery is whatever command the
-- reader configured under [agent] (stdin = one JSON object); delivered_at/error
-- record the hand-off, never the agent's answer.
CREATE TABLE IF NOT EXISTS agent_sends (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reader_id TEXT NOT NULL REFERENCES readers(id),
  by_name TEXT NOT NULL,
  story_id TEXT NOT NULL REFERENCES stories(id),
  note TEXT,                            -- optional, <= 1000 chars
  at INTEGER NOT NULL,
  delivered_at INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_agent_sends_reader ON agent_sends(reader_id, at);

-- v9 (additive): story-based bookmarks ('Guardar', roadmap 6c, DECISIONS 14).
-- A save is about a STORY (multi-source) when saved from the paper (origin
-- 'gaceta'), or about a single ITEM when it comes from an adapter (origin 'x':
-- X bookmarks). Partial unique indexes keep one active save per key; removed
-- saves keep their row (removed_at) so the archive is append-only history.
-- No CHECK on origin so future adapters can join without a migration.
CREATE TABLE IF NOT EXISTS saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reader_id TEXT NOT NULL REFERENCES readers(id),
  by_name TEXT NOT NULL,
  story_id TEXT REFERENCES stories(id),   -- origin 'gaceta' (NULL for 'x')
  item_id TEXT REFERENCES items(id),      -- origin 'x' (NULL for 'gaceta')
  origin TEXT NOT NULL,                   -- 'gaceta' | 'x' | future adapters
  note TEXT,                              -- optional, <= 1000 chars
  at INTEGER NOT NULL,
  removed_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_saves_gaceta
  ON saves(reader_id, by_name, story_id) WHERE removed_at IS NULL AND story_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ix_saves_x
  ON saves(reader_id, item_id) WHERE removed_at IS NULL AND item_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_saves_reader ON saves(reader_id, at);

-- v12 (additive): per-reader daily LLM call accounting ([llm] daily_max_calls).
-- One row per reader per day; day = the reader's local date ([schedule] tz
-- when configured, else system local). Real (non-cache) chat calls only —
-- cache hits are free. Reaching the limit behaves exactly like --max-llm-calls:
-- graceful stop, the edition continues with what is already written.
CREATE TABLE IF NOT EXISTS llm_usage (
  reader_id TEXT NOT NULL REFERENCES readers(id),
  day TEXT NOT NULL,                    -- YYYY-MM-DD, the reader's local day
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (reader_id, day)
);
