"""Persistencia local de fuentes, versiones, aprobaciones e intentos."""

from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    context TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    answer TEXT NOT NULL,
    answer_start INTEGER NOT NULL,
    fragment TEXT NOT NULL,
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS question_versions (
    question_id TEXT NOT NULL REFERENCES questions(id),
    version INTEGER NOT NULL,
    question TEXT NOT NULL,
    PRIMARY KEY (question_id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    question_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (question_id, version),
    FOREIGN KEY (question_id, version) REFERENCES question_versions(question_id, version)
);
CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    response TEXT NOT NULL,
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (question_id, version) REFERENCES approvals(question_id, version)
);
CREATE TABLE IF NOT EXISTS guidance (
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    kind TEXT NOT NULL CHECK (kind IN ('explanation', 'reinforcement')),
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    PRIMARY KEY (attempt_id, kind)
);
"""

REVIEWS = """
SELECT q.id, v.version, v.question, q.answer, q.fragment, q.source_id,
       s.title, s.objective, a.approved_at IS NOT NULL AS approved, q.model
FROM questions AS q
JOIN sources AS s ON s.id = q.source_id
JOIN question_versions AS v ON v.question_id = q.id
LEFT JOIN approvals AS a ON a.question_id = q.id AND a.version = v.version
WHERE v.version = (SELECT MAX(version) FROM question_versions WHERE question_id = q.id)
"""

PRACTICES = """
SELECT q.id, v.version, v.question, s.title, s.objective
FROM questions AS q
JOIN sources AS s ON s.id = q.source_id
JOIN question_versions AS v ON v.question_id = q.id
WHERE v.version = (SELECT MAX(version) FROM approvals WHERE question_id = q.id)
"""

ATTEMPTS = """
SELECT t.id, t.question_id, t.version, t.response, q.answer AS reference,
       q.fragment, v.question
FROM attempts AS t
JOIN questions AS q ON q.id = t.question_id
JOIN question_versions AS v ON v.question_id = q.id AND v.version = t.version
WHERE t.id = ?
"""


@contextmanager
def connect(path: Path, *, write: bool = False):
    with closing(sqlite3.connect(path, timeout=15)) as database, database:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA busy_timeout = 15000")
        database.execute("PRAGMA synchronous = NORMAL")
        database.execute("PRAGMA temp_store = MEMORY")
        database.execute("PRAGMA cache_size = -16000")
        if write:
            database.execute("BEGIN IMMEDIATE")
        yield database


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as database:
        database.execute("PRAGMA journal_mode = WAL")
        database.executescript(SCHEMA)
