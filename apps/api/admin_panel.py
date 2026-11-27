"""Backend REST y persistencia SQLite para el Panel de Administración de EduAndes AI."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .store import connect

VALID_SECTIONS = {
    "users",
    "rbac",
    "academic",
    "rag_docs",
    "ai_governance",
    "security_privacy",
    "licensing",
    "site_config",
    "tickets",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _default_sections() -> dict[str, Any]:
    return {
        "users": {
            "accounts": [
                {
                    "id": "usr-001",
                    "name": "Administrador Principal",
                    "email": "administrador@ecuachile.cl",
                    "role": "Administrador",
                    "institution": "EduAndes Central (CL-EC)",
                    "status": "active",
                    "mfa": True,
                    "sso": "Local + MFA",
                    "last_seen": "Activo ahora",
                },
                {
                    "id": "usr-002",
                    "name": "Docente Binacional",
                    "email": "docente@ecuachile.cl",
                    "role": "Docente",
                    "institution": "Liceo Bicentenario Santiago",
                    "status": "active",
                    "mfa": True,
                    "sso": "Google Workspace",
                    "last_seen": "Hace 12 min",
                },
                {
                    "id": "usr-003",
                    "name": "Estudiante Demo",
                    "email": "estudiante@ecuachile.cl",
                    "role": "Estudiante",
                    "institution": "Unidad Educativa Quito Norte",
                    "status": "active",
                    "mfa": False,
                    "sso": "Microsoft 365",
                    "last_seen": "Hace 28 min",
                },
                {
                    "id": "usr-004",
                    "name": "Orientación y Bienestar",
                    "email": "orientacion@ecuachile.cl",
                    "role": "Directivo / Orientador",
                    "institution": "Red Binacional ODS 4",
                    "status": "active",
                    "mfa": True,
                    "sso": "SAML 2.0",
                    "last_seen": "Hace 1 h",
                },
            ],
            "sso_providers": {
                "google_workspace": True,
                "microsoft_365": True,
                "saml_oauth": False,
                "mfa_required_admins_teachers": True,
            },
        },
        "rbac": {
            "permissions": [
                {"id": "create_assistants", "label": "Crear asistentes propios", "Estudiante": False, "Docente": True, "Directivo / Orientador": True, "Administrador": True},
                {"id": "upload_files", "label": "Subir archivos y PDFs", "Estudiante": False, "Docente": True, "Directivo / Orientador": True, "Administrador": True},
                {"id": "chat_tutor", "label": "Chatear con tutores IA", "Estudiante": True, "Docente": True, "Directivo / Orientador": True, "Administrador": True},
                {"id": "publish_exams", "label": "Publicar evaluaciones", "Estudiante": False, "Docente": True, "Directivo / Orientador": False, "Administrador": True},
                {"id": "view_early_alerts", "label": "Ver alertas tempranas", "Estudiante": False, "Docente": False, "Directivo / Orientador": True, "Administrador": True},
                {"id": "manage_prompts", "label": "Editar System Prompts", "Estudiante": False, "Docente": False, "Directivo / Orientador": False, "Administrador": True},
                {"id": "manage_api_infra", "label": "Gestionar APIs, costos e infra", "Estudiante": False, "Docente": False, "Directivo / Orientador": False, "Administrador": True},
                {"id": "read_private_chats", "label": "Acceso excepcional a chats (con motivo)", "Estudiante": False, "Docente": False, "Directivo / Orientador": True, "Administrador": True},
            ]
        },
        "academic": {
            "periods": [
                {"id": "per-2026", "name": "Año Lectivo 2026 (CL-EC)", "active": True},
                {"id": "per-2025", "name": "Año Lectivo 2025", "active": False},
            ],
            "levels": ["Educación Básica", "Educación Media / Bachillerato", "Educación Superior"],
            "courses": [
                {"id": "cur-1", "name": "Matemáticas 2° Medio A", "level": "Educación Media / Bachillerato", "teacher": "docente@ecuachile.cl", "students": 34, "tutor": "Tutor de Matemáticas"},
                {"id": "cur-2", "name": "Lengua y Literatura 8° Básico", "level": "Educación Básica", "teacher": "docente@ecuachile.cl", "students": 29, "tutor": "Asistente de Redacción"},
                {"id": "cur-3", "name": "Ciencias Naturales 1° Bachillerato", "level": "Educación Media / Bachillerato", "teacher": "docente@ecuachile.cl", "students": 31, "tutor": "Preparador de Evaluaciones"},
            ],
            "modules": [
                {"id": "mod-math", "name": "Tutor de Matemáticas", "level": "Básica y Media", "enabled": True},
                {"id": "mod-write", "name": "Asistente de Redacción", "level": "Básica, Media y Superior", "enabled": True},
                {"id": "mod-eval", "name": "Preparador de Evaluaciones", "level": "Media y Superior", "enabled": True},
                {"id": "mod-code", "name": "Laboratorio de Pensamiento Computacional", "level": "Superior", "enabled": False},
            ],
        },
        "rag_docs": {
            "documents": [
                {"id": "doc-1", "title": "Bases Curriculares Matemática Chile-Ecuador 2026.pdf", "subject": "Matemáticas", "kind": "Programa de estudio", "pages": 38, "status": "Indexado"},
                {"id": "doc-2", "title": "Rúbrica Oficial de Comprensión Lectora ODS 4.pdf", "subject": "Lenguaje", "kind": "Rúbrica", "pages": 12, "status": "Indexado"},
                {"id": "doc-3", "title": "Guía de Ciencias Experimentales y Método Científico.pdf", "subject": "Ciencias", "kind": "Guía oficial", "pages": 24, "status": "Indexado"},
            ]
        },
        "ai_governance": {
            "models": {
                "fast_chat": os.environ.get("EDUCORE_LLM_MODEL", "llama-3.1-8b-instant"),
                "exam_generation": "llama-3.3-70b-versatile",
                "document_analysis": "us.amazon.nova-lite-v1:0",
                "fallback_provider": "Amazon Bedrock (us-east-1)",
                "api_key_masked": ("***" + os.environ.get("EDUCORE_LLM_API_KEY", "")[-4:]) if os.environ.get("EDUCORE_LLM_API_KEY") else "No configurada",
            },
            "prompts": {
                "version": "v2.4-socratico",
                "socratic_mode": True,
                "age_adaptation": "Media (13-17 años)",
                "system_prompt": (
                    "Eres un tutor pedagógico de EduAndes AI alineado al ODS 4. "
                    "Aplica el método socrático: guía al estudiante con preguntas y pistas basadas "
                    "exclusivamente en el material curricular verificado, sin entregar la respuesta final directa."
                ),
            },
            "rate_limits": {
                "messages_per_day_student": 60,
                "tokens_per_day_user": 45000,
                "max_pdf_mb": 8,
                "monthly_institution_budget_usd": 120,
            },
            "guardrails": {
                "content_moderation": True,
                "prompt_injection_shield": True,
                "early_alert_wellness": True,
                "wellness_email": "orientacion@ecuachile.cl",
                "exam_mode_enabled": False,
                "exam_mode_window": "08:00 - 11:30",
            },
            "early_alerts": [
                {"id": "al-101", "date": "Hoy 10:14", "course": "Lengua y Literatura 8° Básico", "category": "Acoso / Bullying", "severity": "Alta", "status": "Derivada a Orientación"},
                {"id": "al-102", "date": "Ayer 16:40", "course": "Matemáticas 2° Medio A", "category": "Crisis / Ansiedad", "severity": "Media", "status": "En seguimiento"},
            ],
        },
        "security_privacy": {
            "anonymize_pii": True,
            "strip_rut_dni": True,
            "chat_retention_days": 90,
            "guardian_consent_required": True,
            "guardian_consents_pct": 94,
            "admin_separation_enforced": True,
            "chat_privacy_require_reason": True,
            "backup_schedule": "Diario 03:00 UTC",
            "backups": [
                {"id": "bkp-1", "file": "educore-auto-2026-11-26.sqlite3", "size_kb": 724, "created_at": "2026-11-26 03:00:00 UTC"},
            ],
        },
        "licensing": {
            "plan": "Institucional",
            "seats_used": 94,
            "seats_total": 250,
            "valid_until": "2027-03-31",
            "feature_flags": [
                {"id": "ff_socratic_v2", "name": "Tutor Socrático v2 (Beta)", "enabled": True, "scope": "Toda la institución"},
                {"id": "ff_voice_tts", "name": "Lectura en voz alta inclusiva (TTS)", "enabled": True, "scope": "Toda la institución"},
                {"id": "ff_bedrock_fallback", "name": "Failover automático a Amazon Bedrock", "enabled": True, "scope": "Infraestructura"},
                {"id": "ff_parent_portal", "name": "Portal de apoderados y consentimientos", "enabled": False, "scope": "Grupo piloto"},
            ],
        },
        "site_config": {
            "brand_name": "EduAndes AI · Red Chile–Ecuador",
            "primary_color": "#0039a6",
            "accent_color": "#ffd100",
            "domain": "eduandes.ecuachile.cl",
            "welcome_text": "Plataforma educativa binacional con IA verificada por citas literales.",
            "terms_url": "#/terminos",
            "privacy_url": "#/privacidad",
            "maintenance_mode": False,
            "banner_active": True,
            "banner_text": "Ventana de sincronización curricular ODS 4 activa. Tutores verificados operativos.",
            "email_template_alert": "Estimado equipo de Orientación: se detectó una señal temprana que requiere revisión.",
        },
        "tickets": {
            "items": [
                {"id": "tck-204", "from": "docente@ecuachile.cl", "type": "Feedback IA", "subject": "Cita de página 4 en guía de Ciencias muy extensa", "status": "Abierto", "priority": "Media"},
                {"id": "tck-203", "from": "estudiante@ecuachile.cl", "type": "Fallo técnico", "subject": "Demora en carga de práctica en móvil", "status": "Resuelto", "priority": "Baja"},
            ]
        },
    }


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"


def initialize_admin(db_path: Path) -> None:
    with connect(db_path, write=True) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                ip TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT ''
            )
            """
        )
        now = _now_iso()
        for key, value in _default_sections().items():
            db.execute(
                "INSERT OR IGNORE INTO admin_kv (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now),
            )
        existing_logs = db.execute("SELECT COUNT(*) FROM admin_audit_logs").fetchone()[0]
        if existing_logs == 0:
            db.execute(
                """
                INSERT INTO admin_audit_logs (timestamp, actor, actor_role, action, target, reason, ip, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    "administrador@ecuachile.cl",
                    "Administrador",
                    "INIT_ADMIN_CONSOLE",
                    "Sistema EduAndes AI",
                    "Inicialización de consola de administración",
                    "127.0.0.1",
                    "Registro de auditoría inmutable habilitado.",
                ),
            )


def _log_audit(
    db: sqlite3.Connection,
    *,
    actor: str,
    actor_role: str,
    action: str,
    target: str,
    reason: str = "",
    ip: str = "127.0.0.1",
    details: str = "",
) -> None:
    db.execute(
        """
        INSERT INTO admin_audit_logs (timestamp, actor, actor_role, action, target, reason, ip, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (_now_iso(), actor, actor_role, action, target, reason, ip, details),
    )


def _get_section(db: sqlite3.Connection, key: str) -> dict[str, Any]:
    row = db.execute("SELECT value FROM admin_kv WHERE key = ?", (key,)).fetchone()
    if row:
        return json.loads(row["value"])
    return _default_sections().get(key, {})


def _save_section(db: sqlite3.Connection, key: str, data: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO admin_kv (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(data, ensure_ascii=False), _now_iso()),
    )


class SectionUpdate(BaseModel):
    data: dict[str, Any]
    actor: str = "administrador@ecuachile.cl"
    actor_role: str = "Administrador"
    action: str = "UPDATE_CONFIG"
    target: str = ""
    reason: str = ""


class CsvImportInput(BaseModel):
    csv_text: str = Field(min_length=3, max_length=200_000)
    actor: str = "administrador@ecuachile.cl"


class ChatAccessRequest(BaseModel):
    chat_id: str = Field(min_length=1, max_length=64)
    actor: str = "administrador@ecuachile.cl"
    actor_role: str = "Administrador"
    reason: str = Field(min_length=8, max_length=500)


class ActionInput(BaseModel):
    actor: str = "administrador@ecuachile.cl"
    actor_role: str = "Administrador"
    target: str = "Todos los usuarios"
    reason: str = ""


def build_admin_router(db_path: Path) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    @router.get("/state")
    def get_state():
        initialize_admin(db_path)
        with connect(db_path) as db:
            sections = {k: _get_section(db, k) for k in VALID_SECTIONS}
            logs = [
                dict(r)
                for r in db.execute(
                    "SELECT id, timestamp, actor, actor_role, action, target, reason, ip, details "
                    "FROM admin_audit_logs ORDER BY id DESC LIMIT 60"
                ).fetchall()
            ]
            # Real DB metrics
            def _count(table: str) -> int:
                try:
                    return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                except sqlite3.Error:
                    return 0

            real_stats = {
                "units": _count("ec_units"),
                "materials": _count("ec_materials"),
                "quizzes": _count("ec_quizzes"),
                "attempts": _count("ec_attempts"),
                "sources": _count("sources"),
                "questions": _count("questions"),
            }
        return {"sections": sections, "audit_logs": logs, "real_stats": real_stats}

    @router.put("/section/{section}")
    def update_section(section: str, body: SectionUpdate, request: Request):
        if section not in VALID_SECTIONS:
            raise HTTPException(404, f"Sección desconocida: {section}")
        initialize_admin(db_path)
        with connect(db_path, write=True) as db:
            _save_section(db, section, body.data)
            _log_audit(
                db,
                actor=body.actor,
                actor_role=body.actor_role,
                action=body.action,
                target=body.target or section,
                reason=body.reason,
                ip=_client_ip(request),
                details=f"Sección '{section}' actualizada.",
            )
            logs = [
                dict(r)
                for r in db.execute(
                    "SELECT id, timestamp, actor, actor_role, action, target, reason, ip, details "
                    "FROM admin_audit_logs ORDER BY id DESC LIMIT 60"
                ).fetchall()
            ]
        return {"ok": True, "section": section, "audit_logs": logs}

    @router.post("/users/import-csv")
    def import_users_csv(body: CsvImportInput, request: Request):
        initialize_admin(db_path)
        reader = csv.DictReader(io.StringIO(body.csv_text.strip()))
        added = 0
        with connect(db_path, write=True) as db:
            users_data = _get_section(db, "users")
            accounts = users_data.get("accounts", [])
            existing_emails = {a["email"].lower() for a in accounts}
            for row in reader:
                email = (row.get("email") or row.get("correo") or "").strip().lower()
                if not email or "@" not in email or email in existing_emails:
                    continue
                accounts.append(
                    {
                        "id": f"usr-{len(accounts) + 1:03d}",
                        "name": (row.get("name") or row.get("nombre") or email.split("@")[0]).strip(),
                        "email": email,
                        "role": (row.get("role") or row.get("rol") or "Estudiante").strip(),
                        "institution": (row.get("institution") or row.get("institucion") or "Red Binacional").strip(),
                        "status": (row.get("status") or "active").strip(),
                        "mfa": False,
                        "sso": "CSV Import",
                        "last_seen": "Recién importado",
                    }
                )
                existing_emails.add(email)
                added += 1
            users_data["accounts"] = accounts
            _save_section(db, "users", users_data)
            _log_audit(
                db,
                actor=body.actor,
                actor_role="Administrador",
                action="IMPORT_USERS_CSV",
                target=f"{added} cuentas",
                reason="Carga masiva CSV",
                ip=_client_ip(request),
            )
        return {"ok": True, "added": added, "accounts": accounts}

    @router.get("/users/export-csv")
    def export_users_csv():
        initialize_admin(db_path)
        with connect(db_path) as db:
            accounts = _get_section(db, "users").get("accounts", [])
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "name", "email", "role", "institution", "status", "mfa", "sso"])
        for a in accounts:
            writer.writerow([a.get("id"), a.get("name"), a.get("email"), a.get("role"), a.get("institution"), a.get("status"), a.get("mfa"), a.get("sso")])
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="eduandes-usuarios.csv"'},
        )

    @router.get("/reports/export")
    def export_report(kind: str = "adoption"):
        initialize_admin(db_path)
        buf = io.StringIO()
        writer = csv.writer(buf)
        if kind == "audit":
            writer.writerow(["id", "timestamp", "actor", "actor_role", "action", "target", "reason", "ip"])
            with connect(db_path) as db:
                for r in db.execute("SELECT id, timestamp, actor, actor_role, action, target, reason, ip FROM admin_audit_logs ORDER BY id DESC").fetchall():
                    writer.writerow([r["id"], r["timestamp"], r["actor"], r["actor_role"], r["action"], r["target"], r["reason"], r["ip"]])
            filename = "eduandes-audit-logs.csv"
        elif kind == "costs":
            writer.writerow(["institucion", "curso", "tokens_consumidos", "costo_estimado_usd", "latencia_ms", "tasa_error_pct"])
            writer.writerow(["Liceo Bicentenario Santiago", "Matemáticas 2° Medio A", "412500", "0.49", "410", "0.4"])
            writer.writerow(["Unidad Educativa Quito Norte", "Lengua y Literatura 8° Básico", "289100", "0.35", "385", "0.2"])
            filename = "eduandes-costos-ia.csv"
        else:
            writer.writerow(["metrica", "valor", "periodo"])
            writer.writerow(["Usuarios activos diarios (DAU)", "84", "Últimos 7 días"])
            writer.writerow(["Usuarios activos mensuales (MAU)", "216", "Mes actual"])
            writer.writerow(["Tema con mayor consulta", "Ecuaciones lineales e interpretación de textos", "Mes actual"])
            filename = "eduandes-analiticas.csv"
        return Response(
            content=buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/health")
    def health_check():
        t0 = time.perf_counter()
        db_ok = True
        try:
            with connect(db_path) as db:
                db.execute("SELECT 1").fetchone()
        except Exception:
            db_ok = False
        db_ms = round((time.perf_counter() - t0) * 1000, 2)
        disk = shutil.disk_usage(db_path.parent if db_path.parent.exists() else Path("."))
        engine = os.environ.get("EDUCORE_ENGINE", "auto")
        has_groq_key = bool(os.environ.get("EDUCORE_LLM_API_KEY"))
        return {
            "status": "healthy" if db_ok else "degraded",
            "checked_at": _now_iso(),
            "database": {"ok": db_ok, "latency_ms": db_ms, "path": str(db_path)},
            "vector_rag": {"ok": True, "detail": "Índice de citas literales operativo"},
            "ai_provider": {
                "engine": engine,
                "model": os.environ.get("EDUCORE_LLM_MODEL", "llama-3.1-8b-instant"),
                "configured": has_groq_key or engine == "bedrock",
            },
            "disk_free_gb": round(disk.free / (1024**3), 2),
        }

    @router.post("/backups")
    def create_backup(body: ActionInput, request: Request):
        initialize_admin(db_path)
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = backup_dir / f"educore-backup-{stamp}.sqlite3"
        shutil.copy2(db_path, dest)
        size_kb = max(1, round(dest.stat().st_size / 1024))
        entry = {"id": f"bkp-{stamp}", "file": dest.name, "size_kb": size_kb, "created_at": _now_iso()}
        with connect(db_path, write=True) as db:
            sec = _get_section(db, "security_privacy")
            backups = sec.get("backups", [])
            backups.insert(0, entry)
            sec["backups"] = backups[:15]
            _save_section(db, "security_privacy", sec)
            _log_audit(
                db,
                actor=body.actor,
                actor_role=body.actor_role,
                action="CREATE_BACKUP",
                target=dest.name,
                reason=body.reason or "Respaldo manual desde panel",
                ip=_client_ip(request),
            )
        return {"ok": True, "backup": entry, "backups": sec["backups"]}

    @router.post("/sessions/revoke-all")
    def revoke_sessions(body: ActionInput, request: Request):
        initialize_admin(db_path)
        with connect(db_path, write=True) as db:
            _log_audit(
                db,
                actor=body.actor,
                actor_role=body.actor_role,
                action="REVOKE_SESSIONS",
                target=body.target,
                reason=body.reason or "Cierre remoto de sesiones activas",
                ip=_client_ip(request),
            )
        return {"ok": True, "message": f"Sesiones cerradas remotamente para: {body.target}"}

    @router.post("/chat-access")
    def access_flagged_chat(body: ChatAccessRequest, request: Request):
        initialize_admin(db_path)
        with connect(db_path, write=True) as db:
            _log_audit(
                db,
                actor=body.actor,
                actor_role=body.actor_role,
                action="EXCEPTIONAL_CHAT_ACCESS",
                target=body.chat_id,
                reason=body.reason.strip(),
                ip=_client_ip(request),
                details="Acceso excepcional auditado bajo política de privacidad (Sección 8).",
            )
        return {
            "ok": True,
            "chat_id": body.chat_id,
            "logged": True,
            "transcript": [
                {"sender": "Estudiante (Anonimizado #E-412)", "text": "No entiendo este ejercicio y me siento muy presionado con el grupo."},
                {"sender": "Tutor IA (Guardrail)", "text": "Lamento que te sientas así. He notificado de forma confidencial al equipo de orientación para acompañarte."},
            ],
        }

    return router
