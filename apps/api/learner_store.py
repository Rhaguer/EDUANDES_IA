"""Local, explicitly selected student records and evidence-scoped histories.

The selected ID is a local workspace scope, not an authentication credential.
Anonymous historical attempts remain unassigned and do not affect any profile.
"""

import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from .store import connect


def now():
    return datetime.now(timezone.utc).isoformat()


class StudentEdit(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    goals: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1200)] = ""
    presentation: Literal["auto", "visual", "steps"] = "auto"


class StudentCreate(StudentEdit):
    course_ids: list[UUID] = Field(default_factory=list, max_length=30)


class EnrollmentInput(BaseModel):
    student_id: UUID


def student(db, identifier):
    row = db.execute("SELECT * FROM ec_students WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, "Estudiante no encontrado. Selecciona un perfil disponible.")
    return {**dict(row), "course_ids": [row[0] for row in db.execute(
        "SELECT course_id FROM ec_enrollments WHERE student_id=? ORDER BY created_at,course_id", (identifier,))]}


def require_enrollment(db, student_id, course_id):
    profile = student(db, student_id)
    if course_id not in profile["course_ids"]:
        raise HTTPException(403, "Este estudiante no está inscrito en la asignatura. Inscríbelo antes de practicar.")
    return profile


def ensure_owner(db, owner, student_id, *, write=False):
    if student_id is not None:
        student(db, student_id)
        if owner != student_id:
            raise HTTPException(404, "Este registro no pertenece al estudiante seleccionado.")
    elif write and owner is not None:
        raise HTTPException(422, "Selecciona el estudiante al que pertenece este intento.")
    if write and owner is None:
        raise HTTPException(409, "Este intento histórico no tiene estudiante asignado y se conserva solo para consulta. Inicia una práctica con un perfil.")


def histories(db, student_id, course_id, unit_id=None):
    """Load complete immutable evidence for policy, never another learner's rows."""
    require_enrollment(db, student_id, course_id)
    where = "a.student_id=? AND u.course_id=?"
    params = [student_id, course_id]
    if unit_id is not None:
        where += " AND a.unit_id=?"
        params.append(unit_id)
    rows = db.execute(
        "SELECT a.*,u.course_id FROM ec_attempts a JOIN ec_units u ON u.id=a.unit_id WHERE "
        + where + " ORDER BY a.submitted_at DESC,a.rowid DESC", params).fetchall()
    attempts = []
    supports = []
    for row in rows:
        attempt = {**json.loads(row["payload"]), "student_id": row["student_id"], "course_id": row["course_id"]}
        support_rows = db.execute("SELECT payload,parent_id FROM ec_reinforcements WHERE attempt_id=? ORDER BY rowid DESC", (row["id"],)).fetchall()
        attempt_supports = [{**json.loads(support_row["payload"]), "parent_id": support_row["parent_id"]} for support_row in support_rows]
        attempt["reinforcement_id"] = attempt_supports[0]["id"] if attempt_supports else None
        attempts.append(attempt)
        for support in attempt_supports:
            supports.append({**support, "student_id": row["student_id"], "unit_id": row["unit_id"], "course_id": row["course_id"]})
    return attempts, supports


def build_router(service):
    router = APIRouter()

    @router.get("/students")
    def list_students():
        with connect(service.db) as db:
            return [student(db, row[0]) for row in db.execute("SELECT id FROM ec_students ORDER BY name COLLATE NOCASE,created_at,id")]

    @router.post("/students", status_code=201)
    def create_student(request: StudentCreate):
        with connect(service.db, write=True) as db:
            courses = list(dict.fromkeys(str(value) for value in request.course_ids))
            for course_id in courses:
                service.course(db, course_id)
            identifier, timestamp = str(uuid4()), now()
            db.execute("INSERT INTO ec_students VALUES (?,?,?,?,?,?)", (identifier, request.name, request.goals, request.presentation, timestamp, timestamp))
            db.executemany("INSERT INTO ec_enrollments VALUES (?,?,?)", [(identifier, course_id, timestamp) for course_id in courses])
            return student(db, identifier)

    @router.get("/students/{student_id}")
    def get_student(student_id: UUID):
        with connect(service.db) as db:
            return student(db, str(student_id))

    @router.put("/students/{student_id}")
    def update_student(student_id: UUID, request: StudentEdit):
        with connect(service.db, write=True) as db:
            student(db, str(student_id))
            db.execute("UPDATE ec_students SET name=?,goals=?,presentation=?,updated_at=? WHERE id=?", (request.name, request.goals, request.presentation, now(), str(student_id)))
            return student(db, str(student_id))

    @router.get("/courses/{course_id}/students")
    def enrolled_students(course_id: UUID):
        with connect(service.db) as db:
            service.course(db, str(course_id))
            return [student(db, row[0]) for row in db.execute(
                "SELECT s.id FROM ec_students s JOIN ec_enrollments e ON e.student_id=s.id WHERE e.course_id=? ORDER BY s.name COLLATE NOCASE,s.created_at,s.id", (str(course_id),))]

    @router.post("/courses/{course_id}/students", status_code=201)
    def enroll_student(course_id: UUID, request: EnrollmentInput):
        with connect(service.db, write=True) as db:
            service.course(db, str(course_id))
            student(db, str(request.student_id))
            db.execute("INSERT INTO ec_enrollments VALUES (?,?,?) ON CONFLICT DO NOTHING", (str(request.student_id), str(course_id), now()))
            return student(db, str(request.student_id))

    @router.get("/students/{student_id}/progress")
    def get_progress(student_id: UUID, course_id: UUID, unit_id: UUID | None = None):
        from .learning_profile import summarize_progress
        with connect(service.db) as db:
            profile = require_enrollment(db, str(student_id), str(course_id))
            service.course(db, str(course_id))
            if unit_id is not None and service.unit(db, str(unit_id))["course_id"] != str(course_id):
                raise HTTPException(422, "La unidad no pertenece a esta asignatura.")
            attempts, supports = histories(db, str(student_id), str(course_id), str(unit_id) if unit_id else None)
            result = summarize_progress(profile, attempts, supports)
            return {**result, "student": profile, "course_id": str(course_id), "unit_id": str(unit_id) if unit_id else None,
                    "attempts": [{key: value for key, value in attempt.items() if key != "items"} for attempt in attempts],
                    "reinforcements": [{key: value for key, value in support.items() if key not in {"private_exercise", "model"}} for support in supports]}

    return router
