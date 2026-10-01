"""Schema migrations. Append (version, sql) tuples; never edit a released one."""

NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ','now'))"

DDL_V1 = f"""
CREATE TABLE task (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL CHECK (length(trim(title)) > 0),
  description TEXT NOT NULL DEFAULT '',
  next_action TEXT,
  kind        TEXT CHECK (kind IS NULL OR kind IN ('do','reply','review','decide','follow_up','investigate')),
  project     TEXT,
  priority    TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent','high','normal','low')),
  due         TEXT CHECK (due IS NULL OR due GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  due_hint    TEXT,
  state       TEXT NOT NULL DEFAULT 'inbox'
              CHECK (state IN ('inbox','todo','doing','waiting','in_review','done','dropped','following')),
  waiting_on  TEXT,
  needs_title INTEGER NOT NULL DEFAULT 0 CHECK (needs_title IN (0,1)),
  triaged_at  TEXT,
  created_at  TEXT NOT NULL DEFAULT {NOW},
  updated_at  TEXT NOT NULL DEFAULT {NOW},
  state_at    TEXT NOT NULL DEFAULT {NOW}
);
CREATE INDEX task_state ON task(state);

CREATE TABLE link (
  id         INTEGER PRIMARY KEY,
  task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  position   INTEGER NOT NULL,
  kind       TEXT NOT NULL CHECK (kind IN ('jira','slack','github','url')),
  url        TEXT,
  ref        TEXT,
  quote      TEXT,
  author     TEXT,
  role       TEXT CHECK (role IS NULL OR role IN ('respond','source','ticket','deliverable','reference')),
  note       TEXT,
  title      TEXT,
  status     TEXT,
  fetched_at TEXT,
  stack          TEXT,
  stack_position INTEGER,
  role_fixed     INTEGER NOT NULL DEFAULT 0 CHECK (role_fixed IN (0,1)),
  CHECK (url IS NOT NULL OR ref IS NOT NULL),
  UNIQUE (task_id, position)
);
CREATE INDEX link_ref ON link(ref);
CREATE INDEX link_stack ON link(stack);

CREATE TABLE person (
  task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  name    TEXT NOT NULL COLLATE NOCASE CHECK (length(trim(name)) > 0),
  PRIMARY KEY (task_id, name)
);

CREATE TABLE entry (
  id      INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  at      TEXT NOT NULL DEFAULT {NOW},
  kind    TEXT NOT NULL CHECK (kind IN ('note','state','triage','jira','slack','followup')),
  text    TEXT NOT NULL
);
CREATE INDEX entry_task ON entry(task_id, at);

-- Who has been asked to review a pull request link, and where each reviewer stands.
CREATE TABLE review (
  link_id  INTEGER NOT NULL REFERENCES link(id) ON DELETE CASCADE,
  reviewer TEXT NOT NULL COLLATE NOCASE,
  state    TEXT NOT NULL
           CHECK (state IN ('requested','approved','changes_requested','commented','dismissed')),
  team     INTEGER NOT NULL DEFAULT 0 CHECK (team IN (0,1)),
  you      INTEGER NOT NULL DEFAULT 0 CHECK (you IN (0,1)),
  PRIMARY KEY (link_id, reviewer)
);

-- Things you owe someone about a task: due on a date, or when the task reaches a state.
CREATE TABLE followup (
  id           INTEGER PRIMARY KEY,
  task_id      INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  action       TEXT NOT NULL CHECK (length(trim(action)) > 0),
  person       TEXT,
  due          TEXT CHECK (due IS NULL OR due GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  on_state     TEXT CHECK (on_state IS NULL OR on_state IN ('inbox','todo','doing','waiting','in_review','done','dropped','following')),
  unless_state TEXT CHECK (unless_state IS NULL OR unless_state IN ('inbox','todo','doing','waiting','in_review','done','dropped','following')),
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','dropped')),
  by_you       INTEGER NOT NULL DEFAULT 0 CHECK (by_you IN (0,1)),
  created_at   TEXT NOT NULL DEFAULT {NOW},
  closed_at    TEXT
);
CREATE INDEX followup_open ON followup(status, due);
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, DDL_V1),
]
LATEST_VERSION = MIGRATIONS[-1][0]
