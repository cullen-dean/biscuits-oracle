-- Biscuits Oracle database schema.
-- Safe to run more than once (IF NOT EXISTS everywhere).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS venues (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    city    TEXT,
    state   TEXT,
    country TEXT,
    UNIQUE (name, city, state)
);

-- Tours: from the API if it ever provides them, otherwise derived from date gaps.
CREATE TABLE IF NOT EXISTS tours (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('api', 'gap'))
);

-- Runs: back-to-back shows at the same venue.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    venue_id   INTEGER NOT NULL REFERENCES venues(id),
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    n_shows    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS shows (
    id              INTEGER PRIMARY KEY,
    api_id          TEXT UNIQUE,              -- UUID from get_shows
    slug            TEXT NOT NULL UNIQUE,
    date            TEXT NOT NULL,            -- YYYY-MM-DD, always from the date field, never the slug
    day_order       INTEGER,                  -- for two shows on one day
    venue_id        INTEGER REFERENCES venues(id),
    tour_id         INTEGER REFERENCES tours(id),
    run_id          INTEGER REFERENCES runs(id),
    night_of_run    INTEGER,
    show_number     INTEGER,                  -- order among counted shows (gap math)
    count_for_stats INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    rating_avg      REAL,
    ratings_count   INTEGER,
    relisten_url    TEXT,
    setlist_hash    TEXT,                     -- detects setlist changes after posting
    first_seen_at   TEXT NOT NULL,
    last_changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shows_date ON shows(date);

CREATE TABLE IF NOT EXISTS sets (
    id        INTEGER PRIMARY KEY,
    show_id   INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    label     TEXT NOT NULL,                  -- S1, S2, S3, E1, E2 ...
    set_order INTEGER NOT NULL,
    set_type  TEXT NOT NULL DEFAULT 'electric'
              CHECK (set_type IN ('electric', 'tractorbeam', 'powered_down', 'side_project')),
    UNIQUE (show_id, label)
);

CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    original_artist TEXT,
    is_cover        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS performances (
    id               INTEGER PRIMARY KEY,
    api_id           TEXT UNIQUE,             -- track UUID
    show_id          INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    set_id           INTEGER NOT NULL REFERENCES sets(id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    song_id          INTEGER NOT NULL REFERENCES songs(id),
    segue_out        INTEGER NOT NULL DEFAULT 0,  -- 1 = ">" into the next track
    is_tractorbeam   INTEGER NOT NULL DEFAULT 0,
    note             TEXT,
    annotations_json TEXT,                    -- raw annotations, kept for re-parsing
    duration_sec     INTEGER,
    all_timer        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (set_id, position)
);
CREATE INDEX IF NOT EXISTS idx_perf_song ON performances(song_id);
CREATE INDEX IF NOT EXISTS idx_perf_show ON performances(show_id);

CREATE TABLE IF NOT EXISTS segues (
    from_perf_id INTEGER NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    to_perf_id   INTEGER NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    PRIMARY KEY (from_perf_id, to_perf_id)
);

-- INVERTED, UNFINISHED, DYSLEXIC, BEGINNING_ONLY, MIDDLE_ONLY, ENDING_ONLY, or anything new the API adds.
CREATE TABLE IF NOT EXISTS performance_flags (
    perf_id INTEGER NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    flag    TEXT NOT NULL,
    PRIMARY KEY (perf_id, flag)
);

-- "completing" finishes "completed". The target is stored raw too, in case
-- the completed show hasn't been loaded yet; completed_perf_id is filled in later.
CREATE TABLE IF NOT EXISTS completions (
    completing_perf_id INTEGER NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    target_show_slug   TEXT NOT NULL,
    target_set         TEXT NOT NULL,
    target_position    INTEGER NOT NULL,
    completed_perf_id  INTEGER REFERENCES performances(id) ON DELETE SET NULL,
    PRIMARY KEY (completing_perf_id, target_show_slug, target_set, target_position)
);

-- Teases, samples and quoted jams parsed from annotations.
CREATE TABLE IF NOT EXISTS teases (
    id      INTEGER PRIMARY KEY,
    perf_id INTEGER NOT NULL REFERENCES performances(id) ON DELETE CASCADE,
    kind    TEXT NOT NULL CHECK (kind IN ('tease', 'sample', 'jam')),
    title   TEXT NOT NULL,
    artist  TEXT,
    song_id INTEGER REFERENCES songs(id)      -- set when it's a Biscuits song we know
);

CREATE TABLE IF NOT EXISTS musicians (
    id   INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

-- Who was on stage for the whole show (used for eras and lineup checks).
CREATE TABLE IF NOT EXISTS show_lineup (
    show_id     INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    musician_id INTEGER NOT NULL REFERENCES musicians(id),
    instruments TEXT,
    PRIMARY KEY (show_id, musician_id)
);

-- Guests. perf_id NULL = whole show.
CREATE TABLE IF NOT EXISTS sit_ins (
    id          INTEGER PRIMARY KEY,
    show_id     INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    perf_id     INTEGER REFERENCES performances(id) ON DELETE CASCADE,
    musician_id INTEGER NOT NULL REFERENCES musicians(id),
    instruments TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id                 INTEGER PRIMARY KEY,
    show_date          TEXT NOT NULL,
    show_id            INTEGER REFERENCES shows(id),
    author             TEXT NOT NULL CHECK (author IN ('me', 'robot')),
    file_path          TEXT NOT NULL UNIQUE,
    committed_at       TEXT,                  -- git commit time; must be before the show
    predicted_json     TEXT NOT NULL,
    actual_json        TEXT,
    score              INTEGER,
    hit_rate           REAL,
    score_details_json TEXT,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'provisional', 'final', 'late'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,              -- backfill / update
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    shows_added   INTEGER DEFAULT 0,
    shows_changed INTEGER DEFAULT 0,
    detail        TEXT,
    error         TEXT
);

-- Backfill progress, so an interrupted backfill can resume.
CREATE TABLE IF NOT EXISTS backfill_progress (
    slug       TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('listed', 'done', 'skipped', 'error')),
    detail     TEXT,
    updated_at TEXT NOT NULL
);
