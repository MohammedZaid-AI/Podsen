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
	itunes_id VARCHAR NOT NULL, 
	fetched FLOAT NOT NULL, 
	payload TEXT NOT NULL, 
	PRIMARY KEY (itunes_id)
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
	PRIMARY KEY (id)
);

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
	PRIMARY KEY (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS ix_feedback_run_id ON feedback (run_id);

-- Lock every table to the API: RLS on, no policies.

ALTER TABLE "apple_episodes_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "feed_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "trending_cache" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "users" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "heard" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "runs" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "shows" ENABLE ROW LEVEL SECURITY;

ALTER TABLE "feedback" ENABLE ROW LEVEL SECURITY;
