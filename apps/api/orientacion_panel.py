"""Panel exclusivo para Directivo / Orientador:
trazabilidad académica, detección temprana de rezago, alertas de bienestar
y bitácora de intervenciones con privacidad segregada por rol."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .security_auth import client_ip, get_session_user
from .store import connect


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class AlertStatusInput(BaseModel):
    status: str = Field(min_length=2, max_length=60)
    note: str = Field(default="", max_length=500)


class InterventionInput(BaseModel):
    student_name: str = Field(min_length=2, max_length=120)
    course_label: str = Field(default="General", max_length=120)
    kind: str = Field(min_length=2, max_length=60)
    summary: str = Field(min_length=4, max_length=800)


def _require_counselor(db_path: Path, request: Request) -> dict[str, Any]:
    user = get_session_user(db_path, request)
    if not user:
        raise HTTPException(401, "Sesión requerida.")
    if user.get("role") not in {"Directivo / Orientador", "Administrador"}:
        raise HTTPException(403, "Acceso exclusivo para Directivo / Orientador.")
    return user


def _load_kv(db, key: str, default: Any) -> Any:
    row = db.execute("SELECT value FROM admin_kv WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def _save_kv(db, key: str, value: Any) -> None:
    db.execute(
        """
        INSERT INTO admin_kv (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), _now_iso()),
    )


def _log_audit(db, *, actor: str, actor_role: str, action: str, target: str, reason: str, ip: str) -> None:
    db.execute(
        """
        INSERT INTO admin_audit_logs (ts, actor, actor_role, action, target, reason, ip, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (_now_iso(), actor, actor_role, action, target, reason, ip, "Acción registrada desde Panel de Orientación."),
    )


def _default_interventions() -> list[dict[str, Any]]:
    return [
        {
            "id": "int-301",
            "ts": "Hoy 09:30 UTC",
            "student_name": "Estudiante Demo",
            "course_label": "EDU2026MAT · Matemáticas",
            "kind": "Plan de refuerzo",
            "summary": "Activación de práctica socrática guiada con AndesBot para reforzar probabilidad condicional.",
            "status": "Activo",
            "author": "orientacion@ecuachile.cl",
        }
    ]


def build_orientacion_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/orientacion", tags=["orientacion"])

    @router.get("/state")
    def get_orientacion_state(request: Request):
        user = _require_counselor(db_path, request)
        with connect(db_path) as db:
            courses = [
                dict(r)
                for r in db.execute(
                    """
                    SELECT c.id, c.code, c.title,
                           COUNT(DISTINCT u.id) AS units_count,
                           COUNT(DISTINCT m.id) AS materials_count,
                           COUNT(DISTINCT p.id) AS pages_count
                    FROM ec_courses c
                    LEFT JOIN ec_units u ON u.course_id = c.id
                    LEFT JOIN ec_materials m ON m.unit_id = u.id
                    LEFT JOIN ec_pages p ON p.material_id = m.id
                    GROUP BY c.id
                    ORDER BY c.code
                    """
                ).fetchall()
            ]
            course_by_id = {c["id"]: c for c in courses}

            students_rows = db.execute(
                "SELECT id, name, goals, presentation, updated_at FROM ec_students ORDER BY name"
            ).fetchall()

            attempts_rows = db.execute(
                """
                SELECT a.id, a.student_id, a.unit_id, a.payload, a.submitted_at, u.course_id
                FROM ec_attempts a
                LEFT JOIN ec_units u ON u.id = a.unit_id
                ORDER BY a.submitted_at DESC
                """
            ).fetchall()

            reinf_rows = db.execute(
                "SELECT a.student_id, COUNT(*) AS cnt FROM ec_reinforcements r "
                "JOIN ec_attempts a ON a.id = r.attempt_id GROUP BY a.student_id"
            ).fetchall()
            reinf_by_student = {r["student_id"]: r["cnt"] for r in reinf_rows}

            enroll_rows = db.execute("SELECT student_id, course_id FROM ec_enrollments").fetchall()
            enroll_by_student: dict[str, list[str]] = {}
            for e in enroll_rows:
                enroll_by_student.setdefault(e["student_id"], []).append(e["course_id"])

            # Aggregate attempts per student and per course
            student_stats: dict[str, dict[str, int]] = {}
            course_stats: dict[str, dict[str, int]] = {}
            for a in attempts_rows:
                sid = a["student_id"] or ""
                cid = a["course_id"] or ""
                try:
                    payload = json.loads(a["payload"] or "{}")
                except Exception:
                    payload = {}
                score = payload.get("score")
                total = payload.get("total")
                if isinstance(score, (int, float)) and isinstance(total, (int, float)) and total > 0:
                    correct = int(score)
                    questions = int(total)
                else:
                    items = payload.get("items") or payload.get("results") or []
                    questions = len(items) if isinstance(items, list) else 0
                    correct = sum(1 for it in items if isinstance(it, dict) and it.get("correct"))

                st = student_stats.setdefault(sid, {"attempts": 0, "correct": 0, "questions": 0})
                st["attempts"] += 1
                st["correct"] += correct
                st["questions"] += questions

                if cid:
                    cs = course_stats.setdefault(cid, {"attempts": 0, "correct": 0, "questions": 0})
                    cs["attempts"] += 1
                    cs["correct"] += correct
                    cs["questions"] += questions

            students_out: list[dict[str, Any]] = []
            at_risk_count = 0
            accuracy_samples: list[float] = []

            for s in students_rows:
                sid = s["id"]
                st = student_stats.get(sid, {"attempts": 0, "correct": 0, "questions": 0})
                acc = round((st["correct"] / st["questions"]) * 100, 1) if st["questions"] > 0 else None
                if acc is not None:
                    accuracy_samples.append(acc)
                if st["attempts"] == 0:
                    risk = "Sin actividad"
                elif acc is not None and acc < 60:
                    risk = "Alto"
                    at_risk_count += 1
                elif acc is not None and acc < 78:
                    risk = "Medio"
                    at_risk_count += 1
                else:
                    risk = "Bajo"

                enrolled_codes = [course_by_id[cid]["code"] for cid in enroll_by_student.get(sid, []) if cid in course_by_id]
                students_out.append(
                    {
                        "id": sid,
                        "name": s["name"],
                        "goals": s["goals"] or "",
                        "presentation": s["presentation"] or "auto",
                        "courses": enrolled_codes,
                        "attempts_count": st["attempts"],
                        "accuracy_pct": acc,
                        "reinforcements_count": reinf_by_student.get(sid, 0),
                        "risk_level": risk,
                        "updated_at": s["updated_at"],
                    }
                )

            courses_out: list[dict[str, Any]] = []
            for c in courses:
                cs = course_stats.get(c["id"], {"attempts": 0, "correct": 0, "questions": 0})
                acc = round((cs["correct"] / cs["questions"]) * 100, 1) if cs["questions"] > 0 else None
                courses_out.append(
                    {
                        **c,
                        "attempts_count": cs["attempts"],
                        "accuracy_pct": acc,
                    }
                )

            ai_gov = _load_kv(db, "ai_governance", {})
            early_alerts = list(ai_gov.get("early_alerts") or [])
            active_alerts = [a for a in early_alerts if str(a.get("status", "")).lower() not in {"resuelta", "cerrada"}]

            interventions = _load_kv(db, "orientacion_interventions", _default_interventions())

            avg_mastery = round(sum(accuracy_samples) / len(accuracy_samples), 1) if accuracy_samples else None

        return {
            "user": {
                "name": user.get("name"),
                "email": user.get("email"),
                "role": user.get("role"),
                "institution": user.get("institution"),
            },
            "kpis": {
                "students_total": len(students_out),
                "at_risk_count": at_risk_count,
                "active_alerts_count": len(active_alerts),
                "avg_mastery_pct": avg_mastery,
                "attempts_total": len(attempts_rows),
                "reinforcements_total": sum(reinf_by_student.values()),
                "interventions_total": len(interventions),
            },
            "students": students_out,
            "courses": courses_out,
            "early_alerts": early_alerts,
            "interventions": interventions,
        }

    @router.post("/alerts/{alert_id}/status")
    def update_alert_status(alert_id: str, body: AlertStatusInput, request: Request):
        user = _require_counselor(db_path, request)
        allowed_statuses = {"Derivada a Orientación", "En seguimiento", "Entrevista agendada", "Resuelta"}
        if body.status not in allowed_statuses:
            raise HTTPException(422, "Estado de alerta no válido.")

        with connect(db_path, write=True) as db:
            ai_gov = _load_kv(db, "ai_governance", {})
            alerts = list(ai_gov.get("early_alerts") or [])
            found = None
            for al in alerts:
                if al.get("id") == alert_id:
                    al["status"] = body.status
                    if body.note.strip():
                        al["note"] = body.note.strip()
                    found = al
                    break
            if not found:
                raise HTTPException(404, "Alerta temprana no encontrada.")
            ai_gov["early_alerts"] = alerts
            _save_kv(db, "ai_governance", ai_gov)
            _log_audit(
                db,
                actor=user["email"],
                actor_role=user["role"],
                action="ORIENTACION_ALERT_UPDATE",
                target=alert_id,
                reason=f"Estado: {body.status}",
                ip=client_ip(request),
            )
        return {"ok": True, "early_alerts": alerts}

    @router.post("/interventions")
    def create_intervention(body: InterventionInput, request: Request):
        user = _require_counselor(db_path, request)
        item = {
            "id": f"int-{secrets.token_hex(3)}",
            "ts": _now_iso(),
            "student_name": body.student_name.strip(),
            "course_label": body.course_label.strip() or "General",
            "kind": body.kind.strip(),
            "summary": body.summary.strip(),
            "status": "Activo",
            "author": user["email"],
        }
        with connect(db_path, write=True) as db:
            interventions = _load_kv(db, "orientacion_interventions", _default_interventions())
            interventions.insert(0, item)
            _save_kv(db, "orientacion_interventions", interventions[:100])
            _log_audit(
                db,
                actor=user["email"],
                actor_role=user["role"],
                action="ORIENTACION_INTERVENTION",
                target=item["student_name"],
                reason=f"{item['kind']} ({item['course_label']})",
                ip=client_ip(request),
            )
        return {"ok": True, "interventions": interventions[:100]}

    return router
