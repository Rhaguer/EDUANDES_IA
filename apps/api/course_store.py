"""Additive course storage; publications and learner attempts are immutable snapshots."""

from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS ec_courses (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, code TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ec_students (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, goals TEXT NOT NULL,
 presentation TEXT NOT NULL CHECK(presentation IN ('auto','visual','steps')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ec_enrollments (
 student_id TEXT NOT NULL REFERENCES ec_students(id),
 course_id TEXT NOT NULL REFERENCES ec_courses(id), created_at TEXT NOT NULL,
 PRIMARY KEY(student_id,course_id)
);
CREATE TABLE IF NOT EXISTS ec_units (
 id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES ec_courses(id),
 title TEXT NOT NULL, position INTEGER NOT NULL, objectives TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ec_materials (
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES ec_units(id),
 filename TEXT NOT NULL, kind TEXT NOT NULL, sha256 TEXT NOT NULL,
 page_count INTEGER NOT NULL, created_at TEXT NOT NULL, storage_name TEXT NOT NULL,
 UNIQUE(unit_id, sha256)
);
CREATE TABLE IF NOT EXISTS ec_pages (
 id TEXT PRIMARY KEY, material_id TEXT NOT NULL REFERENCES ec_materials(id),
 number INTEGER NOT NULL, label TEXT NOT NULL, text TEXT NOT NULL,
 UNIQUE(material_id, number)
);
CREATE TABLE IF NOT EXISTS ec_quizzes (
 id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES ec_units(id),
 status TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
 generation_round INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ec_publications (
 quiz_id TEXT NOT NULL REFERENCES ec_quizzes(id), version INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(quiz_id,version)
);
CREATE TABLE IF NOT EXISTS ec_attempts (
 id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL REFERENCES ec_quizzes(id),
 unit_id TEXT NOT NULL REFERENCES ec_units(id), version INTEGER NOT NULL,
 submitted_at TEXT NOT NULL, input TEXT NOT NULL, payload TEXT NOT NULL,
 FOREIGN KEY(quiz_id,version) REFERENCES ec_publications(quiz_id,version)
);
CREATE TABLE IF NOT EXISTS ec_reinforcements (
 id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES ec_attempts(id),
 status TEXT NOT NULL, payload TEXT NOT NULL,
 response_request_id TEXT UNIQUE, response_input TEXT,
 parent_id TEXT UNIQUE REFERENCES ec_reinforcements(id)
);
CREATE INDEX IF NOT EXISTS ec_material_unit ON ec_materials(unit_id);
CREATE INDEX IF NOT EXISTS ec_quiz_unit ON ec_quizzes(unit_id);
CREATE INDEX IF NOT EXISTS ec_attempt_unit ON ec_attempts(unit_id, submitted_at);
"""


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as database:
        database.executescript(SCHEMA)
        # Serialize additive migrations when more than one worker starts at once.
        database.execute("BEGIN IMMEDIATE")
        if "generation_round" not in {row["name"] for row in database.execute("PRAGMA table_info(ec_quizzes)")}:
            database.execute("ALTER TABLE ec_quizzes ADD COLUMN generation_round INTEGER NOT NULL DEFAULT 0")
        # Older demonstration attempts deliberately keep a NULL owner. A migration
        # must never attribute anonymous history to a newly created student.
        if "student_id" not in {row["name"] for row in database.execute("PRAGMA table_info(ec_attempts)")}:
            database.execute("ALTER TABLE ec_attempts ADD COLUMN student_id TEXT REFERENCES ec_students(id)")
        database.execute("CREATE INDEX IF NOT EXISTS ec_attempt_student ON ec_attempts(student_id,unit_id,submitted_at)")
        if "parent_id" not in {row["name"] for row in database.execute("PRAGMA table_info(ec_reinforcements)")}:
            # SQLite cannot drop the old inline UNIQUE(attempt_id). Rebuild in
            # this transaction, retaining row order and exact saved responses.
            database.execute("""CREATE TABLE ec_reinforcements_rounds (
                id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES ec_attempts(id),
                status TEXT NOT NULL, payload TEXT NOT NULL,
                response_request_id TEXT UNIQUE, response_input TEXT,
                parent_id TEXT UNIQUE REFERENCES ec_reinforcements_rounds(id)
            )""")
            database.execute("""INSERT INTO ec_reinforcements_rounds
                (id,attempt_id,status,payload,response_request_id,response_input)
                SELECT id,attempt_id,status,payload,response_request_id,response_input
                FROM ec_reinforcements ORDER BY rowid""")
            database.execute("DROP TABLE ec_reinforcements")
            database.execute("ALTER TABLE ec_reinforcements_rounds RENAME TO ec_reinforcements")
        database.execute("CREATE UNIQUE INDEX IF NOT EXISTS ec_reinforcement_root ON ec_reinforcements(attempt_id) WHERE parent_id IS NULL")
        database.execute("CREATE INDEX IF NOT EXISTS ec_reinforcement_attempt ON ec_reinforcements(attempt_id)")

