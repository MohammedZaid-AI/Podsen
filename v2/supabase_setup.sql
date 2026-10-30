-- Podsen: database setup for Supabase Postgres.
--
-- Run this ONCE in the Supabase dashboard: SQL Editor -> New query -> paste -> Run.
-- Safe to run again: every statement is IF NOT EXISTS / idempotent.
--
-- Row Level Security is enabled on every table with NO policies. That means the
-- public anon key (and any signed-in user token) can read or write NOTHING here
-- through the Supabase REST API. The Podsen server connects directly with
-- DATABASE_URL as the table owner, which RLS does not restrict.
--
-- Generated from the table definitions in v2/db.py; keep the two in step.

CREATE TABLE IF NOT EXISTS apple_episodes_cache (

CREATE TABLE IF NOT EXISTS apple_episodes_cache (
	itunes_id VARCHAR NOT NULL, 
	fetched FLOAT NOT NULL, 
	payload TEXT NOT NULL, 
	PRIMARY KEY (itunes_id)
);

CREATE TABLE IF NOT EXISTS episodes (
	feed_url TEXT NOT NULL, 
	guid TEXT NOT NULL, 
	title TEXT, 
	description TEXT, 
	released VARCHAR, 
	stored FLOAT NOT NULL, 
	PRIMARY KEY (feed_url, guid)
);

CREATE TABLE IF NOT EXISTS feed_cache (
	feed_url TEXT NOT NULL, 
	fetched FLOAT NOT NULL, 
	payload TEXT NOT NULL, 
	PRIMARY KEY (feed_url)
);

CREATE TABLE IF NOT EXISTS trending_cache (
	country VARCHAR NOT NULL, 
	fetched FLOAT NOT NULL, 
	payload TEXT NOT NULL, 
	PRIMARY KEY (country)
);

CREATE TABLE IF NOT EXISTS users (
	id VARCHAR NOT NULL, 
	email VARCHAR NOT NULL, 
	created FLOAT NOT NULL, 
	avatar_url VARCHAR, 
	PRIMARY KEY (id)
);
-- Added later; the app also runs this on start.
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;

CREATE INDEX IF NOT EXISTS ix_users_email ON users (email);

CREATE TABLE IF NOT EXISTS heard (
	user_id VARCHAR NOT NULL, 
	episode_id TEXT NOT NULL, 
	ts FLOAT NOT NULL, 
	PRIMARY KEY (user_id, episode_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS runs (
	id VARCHAR NOT NULL, 
	user_id VARCHAR NOT NULL, 
	ts FLOAT NOT NULL, 
	budget INTEGER NOT NULL, 
	engine VARCHAR NOT NULL, 
	payload TEXT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_runs_user_id ON runs (user_id);

CREATE TABLE IF NOT EXISTS shows (
	user_id VARCHAR NOT NULL, 
	itunes_id VARCHAR NOT NULL, 
	name TEXT NOT NULL, 
	artist TEXT, 
	feed_url TEXT NOT NULL, 
	artwork_url TEXT, 
	apple_url TEXT, 
	added FLOAT NOT NULL, 
	PRIMARY KEY (user_id, itunes_id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS feedback (
	id SERIAL NOT NULL, 
	run_id VARCHAR NOT NULL, 
	user_id VARCHAR NOT NULL, 
	ts FLOAT NOT NULL, 
	verdict VARCHAR NOT NULL, 
	budget INTEGER NOT NULL, 
	engine VARCHAR NOT NULL, 
	picks TEXT NOT NULL, 
	note TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

-- Added after launch: the optional feedback note. For a database created before it,
-- this adds the column; on a fresh one it does nothing. Run it BEFORE deploying code
-- that reads feedback.note.
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS note TEXT;

CREATE INDEX IF NOT EXISTS ix_feedback_run_id ON feedback (run_id);

-- Listening preferences, one row per user (v2/preferences.py). wants and skips are
-- JSON lists stored as TEXT, e.g. '["learning","business"]'. Added after launch.
CREATE TABLE IF NOT EXISTS preferences (
	user_id VARCHAR NOT NULL,
	listening_speed FLOAT NOT NULL,
	wants TEXT NOT NULL,
	skips TEXT NOT NULL,
	updated_at FLOAT NOT NULL,
	PRIMARY KEY (user_id),
	FOREIGN KEY(user_id) REFERENCES users (id)
);

-- Lock every table to the API: RLS on, no policies.

ALTER TABLE "apple_episodes_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "episodes" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "feed_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "trending_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "users" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "heard" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "runs" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "shows" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "feedback" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "preferences" ENABLE ROW LEVEL SECURITY;
