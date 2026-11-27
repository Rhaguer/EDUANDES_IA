"""API local de EduCore, con inferencia real y persistencia SQLite."""

from contextlib import asynccontextmanager
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StringConstraints, field_validator
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .store import ATTEMPTS, PRACTICES, REVIEWS, connect, initialize
from .courses import CourseService, build_router
from .admin_panel import build_admin_router, initialize_admin
from .native_core import native_status
from .orientacion_panel import build_orientacion_router
from .security_auth import build_auth_router, get_session_user, initialize_auth, is_https_request
from .tutor_chat import build_tutor_router


MAX_CONTEXT = 50_000
MAX_PDF_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 40
FRAGMENT_CHARS = 2400
LOGGER = logging.getLogger(__name__)


class SourceInput(BaseModel):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
    objective: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    context: Annotated[str, StringConstraints(min_length=1, max_length=MAX_CONTEXT)]

    @field_validator("context")
    @classmethod
    def context_has_text(cls, context: str) -> str:
        if not context.strip():
            raise ValueError("La fuente debe contener texto.")
        return context


class QuestionInput(BaseModel):
    source_id: UUID
    answer: Annotated[str, StringConstraints(min_length=1, max_length=1200)]
    answer_start: int = Field(ge=0, strict=True)


class VersionInput(BaseModel):
    version: int = Field(gt=0, strict=True)


class EditInput(VersionInput):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=700)]


class AttemptInput(VersionInput):
    response: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
    request_id: UUID | None = None


def _row(database: sqlite3.Connection, query: str, parameters: tuple, message: str) -> sqlite3.Row:
    result = database.execute(query, parameters).fetchone()
    if result is None:
        raise HTTPException(404, message)
    return result


def _review(database: sqlite3.Connection, question_id: str) -> dict:
    return _review_payload(_row(database, REVIEWS + " AND q.id = ?", (question_id,), "Pregunta no encontrada."))


def _review_payload(row: sqlite3.Row) -> dict:
    return {**dict(row), "approved": bool(row["approved"]), "model": json.loads(row["model"])}


def _fragment(context: str, answer: str, start: int) -> tuple[str, int]:
    end = start + len(answer)
    if not answer.strip() or end > len(context) or context[start:end] != answer:
        raise HTTPException(422, "La respuesta objetivo debe coincidir con el texto seleccionado en la fuente.")
    left = max(0, start - (FRAGMENT_CHARS - len(answer)) // 2)
    right = min(len(context), left + FRAGMENT_CHARS)
    left = max(0, right - FRAGMENT_CHARS)
    return context[left:right], start - left


def _extract_pdf(data: bytes) -> str:
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "El PDF supera el límite de 8 MB.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(422, "El archivo no es un PDF válido.")
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise HTTPException(422, "El PDF está protegido. Usa un PDF sin contraseña.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise HTTPException(413, "Carga una sección del PDF de hasta 40 páginas.")
        pages = []
        length = 0
        for page in reader.pages:
            page_text = page.extract_text().strip()
            if page_text:
                length += len(page_text) + (2 if pages else 0)
                if length > MAX_CONTEXT:
                    raise HTTPException(413, "Carga una sección del PDF de hasta 50.000 caracteres.")
                pages.append(page_text)
    except (PyPdfError, ValueError, TypeError, KeyError, NotImplementedError) as error:
        LOGGER.info("No se pudo leer un PDF", exc_info=True)
        raise HTTPException(422, "El PDF no se pudo leer. Puedes pegar el texto en su lugar.") from error
    if not pages:
        raise HTTPException(422, "El PDF no tiene texto seleccionable. Puedes pegar el texto en su lugar.")
    return "\n\n".join(pages)


REINFORCEMENT_ENGINES = {
    "trained-base": "Qwen3-1.7B (modelo base)",
    "local-4b": "Qwen3-4B-Instruct (4 bits)",
}


def _adapter_identity(adapter_dir: Path) -> tuple[bool, str | None]:
    """Lee solo los archivos del adaptador; no importa torch ni carga pesos."""
    metadata_path = adapter_dir / "metadata.json"
    present = metadata_path.is_file() and (adapter_dir / "adapter_model.safetensors").is_file()
    if not present:
        return False, None
    try:
        version = json.loads(metadata_path.read_text(encoding="utf-8")).get("adapter_version")
    except (OSError, ValueError, AttributeError):
        return True, None
    return True, version if isinstance(version, str) else None


def _base_model_missing_offline(adapter_dir: Path) -> bool:
    """Sin Internet (HF_HUB_OFFLINE=1), indica si falta el modelo base en la caché local.

    Solo revisa archivos: no importa torch ni transformers.
    """
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        return False
    try:
        metadata = json.loads((adapter_dir / "metadata.json").read_text(encoding="utf-8"))
        base_model, revision = metadata["base_model"], metadata["base_revision"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    cache = Path(os.environ.get("HF_HOME", ".cache/huggingface")) / "hub"
    snapshot = cache / f"models--{base_model.replace('/', '--')}" / "snapshots" / revision
    return not (snapshot / "config.json").is_file()


def _resolve_engine_mode() -> str:
    mode = os.environ.get("EDUCORE_ENGINE", "auto").strip().lower()
    if mode in {"bedrock", "openai_compat", "local"}:
        return mode
    if os.environ.get("EDUCORE_BEDROCK_MODEL_ID"):
        return "bedrock"
    if os.environ.get("EDUCORE_LLM_API_KEY"):
        return "openai_compat"
    return "local"


def create_app(db_path: Path, adapter_dir: Path, *, engine=None) -> FastAPI:
    """Solo las pruebas inyectan un motor. El servidor carga el adaptador de disco o motor cloud."""
    load_lock = Lock()
    injected_engine = engine is not None
    reinforcement_engine = None
    # Estado observable sin cargar modelos: lo consulta GET /api/engine/status.
    load_errors: dict[str, str | None] = {"question": None, "reinforcement": None}

    def get_engine():
        nonlocal engine
        with load_lock:
            if engine is None:
                mode = _resolve_engine_mode()
                try:
                    if mode == "bedrock":
                        from .cloud_engines import BedrockEngine

                        loaded = BedrockEngine()
                    elif mode == "openai_compat":
                        from .cloud_engines import OpenAICompatibleEngine

                        loaded = OpenAICompatibleEngine()
                    else:
                        os.environ.setdefault("HF_HOME", str(Path(".cache/huggingface").resolve()))
                        from .learning_engine import LearningEngine

                        loaded = LearningEngine(adapter_dir)
                        if loaded.status()["adapter_active"] is not True:
                            raise RuntimeError("Se requiere un adaptador activo para generar preguntas.")
                    engine = loaded
                    load_errors["question"] = None
                except Exception as error:
                    LOGGER.exception("No se pudo inicializar el motor de EduAndes AI (%s)", mode)
                    load_errors["question"] = f"No se pudo inicializar el motor ({mode}): {error}"
                    raise HTTPException(503, "El motor no pudo cargar el adaptador entrenado.") from error
        return engine

    def get_reinforcement_engine():
        nonlocal reinforcement_engine
        if injected_engine or _resolve_engine_mode() != "local":
            return get_engine()
        selection = os.environ.get("EDUCORE_REINFORCEMENT_ENGINE", "trained-base")
        if selection == "trained-base":
            return get_engine()
        if selection != "local-4b":
            raise HTTPException(503, "La configuración del motor de refuerzos no es válida.")
        with load_lock:
            if reinforcement_engine is None:
                from .reinforcement_engine import LocalReinforcementEngine
                try:
                    cache = Path(os.environ.get("HF_HOME", ".cache/huggingface")) / "hub"
                    reinforcement_engine = LocalReinforcementEngine(cache_dir=cache.resolve())
                    load_errors["reinforcement"] = None
                except Exception as error:
                    LOGGER.exception("No se pudo cargar el motor local de refuerzos")
                    load_errors["reinforcement"] = "No se pudo cargar el modelo local de refuerzos. Revisa su instalación."
                    raise HTTPException(503, "No se pudo cargar el modelo local de refuerzos. Revisa su instalación.") from error
        return reinforcement_engine

    def engine_status():
        """Describe los motores sin cargarlos: nunca llama get_engine ni importa torch."""
        mode = _resolve_engine_mode()
        if not injected_engine and mode in {"bedrock", "openai_compat"}:
            label = "Amazon Bedrock (cloud)" if mode == "bedrock" else "Groq Cloud API"
            model_name = os.environ.get("EDUCORE_BEDROCK_MODEL_ID", "Nova Lite") if mode == "bedrock" else os.environ.get("EDUCORE_LLM_MODEL", "qwen/qwen3.8-27b")
            question = {"state": "listo", "detail": f"Motor cloud activo ({label} · {model_name})."}
            reinforcement = {"name": mode, "label": label, **question}
            return {
                "question_engine": question,
                "reinforcement_engine": reinforcement,
                "adapter_present": False,
                "adapter_version": None,
                "mode": mode,
                "model_name": model_name,
                "native_core": native_status(),
            }

        adapter_present, adapter_version = _adapter_identity(adapter_dir)
        if engine is not None:
            question = {"state": "listo", "detail": None}
        elif load_errors["question"]:
            question = {"state": "error", "detail": load_errors["question"]}
        elif not adapter_present:
            question = {"state": "error", "detail": "No se encontró el adaptador entrenado en la carpeta configurada."}
        elif not injected_engine and _base_model_missing_offline(adapter_dir):
            question = {"state": "error", "detail": "El modelo base Qwen3-1.7B no está en la caché local y no hay conexión."}
        else:
            question = {"state": "sin_cargar", "detail": "El modelo se carga en la primera generación."}
        selection = os.environ.get("EDUCORE_REINFORCEMENT_ENGINE", "trained-base")
        if injected_engine or selection == "trained-base":
            reinforcement = {"name": "trained-base", "label": REINFORCEMENT_ENGINES["trained-base"], **question}
        elif selection not in REINFORCEMENT_ENGINES:
            reinforcement = {"name": selection, "label": selection, "state": "error",
                             "detail": "La configuración del motor de refuerzos no es válida."}
        elif reinforcement_engine is not None:
            reinforcement = {"name": selection, "label": REINFORCEMENT_ENGINES[selection], "state": "listo", "detail": None}
        elif load_errors["reinforcement"]:
            reinforcement = {"name": selection, "label": REINFORCEMENT_ENGINES[selection], "state": "error",
                             "detail": load_errors["reinforcement"]}
        else:
            reinforcement = {"name": selection, "label": REINFORCEMENT_ENGINES[selection], "state": "sin_cargar",
                             "detail": "El modelo se carga en el primer refuerzo."}
        return {"question_engine": question, "reinforcement_engine": reinforcement,
                "adapter_present": adapter_present, "adapter_version": adapter_version}

    course_service = CourseService(db_path, get_engine, get_reinforcement_engine=get_reinforcement_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialize(db_path)
        course_service.initialize()
        initialize_auth(db_path)
        initialize_admin(db_path)
        try:
            yield
        finally:
            course_service.close()

    application = FastAPI(title="EduAndes AI", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    application.add_middleware(GZipMiddleware, minimum_size=500)

    def _is_maintenance_on() -> bool:
        try:
            with connect(db_path) as database:
                row = database.execute("SELECT value FROM admin_kv WHERE key = 'site_config'").fetchone()
                if row:
                    return bool(json.loads(row["value"]).get("maintenance_mode"))
        except Exception:
            pass
        return False

    @application.middleware("http")
    async def security_and_auth_middleware(request: Request, call_next):
        path = request.url.path
        method = request.method.upper()

        # Protección CSRF por Origin en peticiones mutantes a /api/*
        if path.startswith("/api/") and method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if origin and host:
                allowed = {f"https://{host}", f"http://{host}"}
                if origin.rstrip("/") not in allowed:
                    return JSONResponse({"detail": "Origen no autorizado (CSRF)."}, status_code=403)

        # Control de acceso y modo mantenimiento para /api/*
        public_api = path in {"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}
        if path.startswith("/api/") and not public_api:
            user = get_session_user(db_path, request)
            if not user:
                return JSONResponse({"detail": "Sesión requerida."}, status_code=401)
            if path.startswith("/api/admin/") and user["role"] != "Administrador":
                return JSONResponse({"detail": "Requiere rol Administrador."}, status_code=403)
            if path.startswith("/api/orientacion/") and user["role"] not in {"Directivo / Orientador", "Administrador"}:
                return JSONResponse({"detail": "Requiere rol Directivo / Orientador."}, status_code=403)
            if user["role"] == "Directivo / Orientador" and method in {"POST", "PUT", "PATCH", "DELETE"}:
                allowed_counselor_prefixes = ("/api/orientacion/", "/api/auth/", "/api/tutor/")
                if not path.startswith(allowed_counselor_prefixes):
                    return JSONResponse({"detail": "El rol Directivo / Orientador es de solo lectura académica."}, status_code=403)
            if user["role"] != "Administrador" and _is_maintenance_on():
                return JSONResponse({"detail": "Plataforma en modo mantenimiento.", "maintenance": True}, status_code=503)
            # Restringir acciones exclusivas de docente/admin para estudiantes
            if user["role"] == "Estudiante" and method in {"POST", "PUT", "PATCH", "DELETE"}:
                allowed_student_prefixes = ("/api/quiz-attempts", "/api/reinforcements", "/api/attempts", "/api/auth/", "/api/tutor/")
                allowed_student_suffixes = ("/attempts", "/submit", "/reinforcement", "/explanation")
                own_profile_edit = method == "PUT" and path.startswith("/api/students/")
                if not (own_profile_edit or path.startswith(allowed_student_prefixes) or path.endswith(allowed_student_suffixes)):
                    return JSONResponse({"detail": "Acción no permitida para el rol Estudiante."}, status_code=403)

        response = await call_next(request)

        # Cabeceras de seguridad OWASP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "font-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        if is_https_request(request):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.state.course_service = course_service
    application.include_router(build_auth_router(db_path))
    application.include_router(build_router(course_service))
    application.include_router(build_admin_router(db_path))
    application.include_router(build_orientacion_router(db_path))
    application.include_router(build_tutor_router(db_path, get_engine))

    @application.get("/api/health")
    def public_health():
        return {"status": "ok"}

    @application.get("/api/engine/status")
    def light_status():
        return engine_status()

    @application.get("/api/engine")
    def status():
        return get_engine().status()

    @application.post("/api/sources", status_code=201)
    def create_source(source: SourceInput):
        source_id = str(uuid4())
        with connect(db_path, write=True) as database:
            database.execute(
                "INSERT INTO sources (id, title, objective, context) VALUES (?, ?, ?, ?)",
                (source_id, source.title, source.objective, source.context),
            )
        return {"id": source_id, **source.model_dump()}

    @application.post("/api/sources/pdf")
    def extract_pdf(file: Annotated[UploadFile, File()]):
        return {"context": _extract_pdf(file.file.read(MAX_PDF_BYTES + 1))}

    @application.get("/api/sources/{source_id}")
    def get_source(source_id: UUID):
        with connect(db_path) as database:
            source = _row(
                database,
                "SELECT id, title, objective, context FROM sources WHERE id = ?",
                (str(source_id),),
                "Material no encontrado. Vuelve a la lista de prácticas.",
            )
            return dict(source)

    @application.post("/api/questions", status_code=201)
    def create_question(request: QuestionInput):
        source_id = str(request.source_id)
        with connect(db_path) as database:
            source = _row(database, "SELECT context FROM sources WHERE id = ?", (source_id,), "Fuente no encontrada.")
        fragment, start = _fragment(source["context"], request.answer, request.answer_start)
        model = get_engine()
        try:
            question = model.generate(fragment, request.answer, answer_start=start).strip()
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except Exception as error:
            LOGGER.exception("El motor falló al generar una pregunta")
            raise HTTPException(503, "El motor no pudo generar la pregunta. Vuelve a intentarlo.") from error
        if not question:
            raise HTTPException(502, "El motor devolvió una pregunta vacía. Prueba con otro fragmento.")
        question_id = str(uuid4())
        with connect(db_path, write=True) as database:
            database.execute(
                "INSERT INTO questions (id, source_id, answer, answer_start, fragment, model) VALUES (?, ?, ?, ?, ?, ?)",
                (question_id, source_id, request.answer, request.answer_start, fragment, json.dumps(model.status())),
            )
            database.execute("INSERT INTO question_versions VALUES (?, 1, ?)", (question_id, question))
            return _review(database, question_id)

    @application.get("/api/questions")
    def list_reviews():
        with connect(db_path) as database:
            return [_review_payload(row) for row in database.execute(REVIEWS + " ORDER BY q.rowid DESC")]

    @application.get("/api/questions/{question_id}")
    def get_review(question_id: UUID):
        with connect(db_path) as database:
            return _review(database, str(question_id))

    @application.put("/api/questions/{question_id}")
    def edit_question(question_id: UUID, request: EditInput):
        question_key = str(question_id)
        with connect(db_path, write=True) as database:
            current = _review(database, question_key)
            if request.version != current["version"]:
                raise HTTPException(409, "La pregunta cambió. Abre su versión más reciente.")
            database.execute("INSERT INTO question_versions VALUES (?, ?, ?)", (question_key, request.version + 1, request.question))
            return _review(database, question_key)

    @application.post("/api/questions/{question_id}/approve")
    def approve_question(question_id: UUID, request: VersionInput):
        question_key = str(question_id)
        with connect(db_path, write=True) as database:
            if request.version != _review(database, question_key)["version"]:
                raise HTTPException(409, "Solo se puede aprobar la versión más reciente de la pregunta.")
            database.execute(
                "INSERT INTO approvals (question_id, version) VALUES (?, ?) ON CONFLICT (question_id, version) DO NOTHING",
                (question_key, request.version),
            )
            return _review(database, question_key)

    @application.get("/api/practice")
    def list_practice():
        with connect(db_path) as database:
            return [dict(row) for row in database.execute(PRACTICES + " ORDER BY q.rowid DESC")]

    @application.get("/api/practice/{question_id}")
    def get_practice(question_id: UUID):
        with connect(db_path) as database:
            return dict(_row(database, PRACTICES + " AND q.id = ?", (str(question_id),), "Práctica aprobada no encontrada."))

    @application.post("/api/practice/{question_id}/attempts", status_code=201)
    def create_attempt(question_id: UUID, request: AttemptInput):
        question_key = str(question_id)
        attempt_id = str(request.request_id or uuid4())
        with connect(db_path, write=True) as database:
            _row(database, "SELECT 1 FROM approvals WHERE question_id = ? AND version = ?", (question_key, request.version), "Práctica aprobada no encontrada.")
            database.execute(
                "INSERT INTO attempts (id, question_id, version, response) VALUES (?, ?, ?, ?) ON CONFLICT (id) DO NOTHING",
                (attempt_id, question_key, request.version, request.response),
            )
            row = _row(database, ATTEMPTS, (attempt_id,), "Intento no encontrado.")
            if (row["question_id"], row["version"], row["response"]) != (question_key, request.version, request.response):
                raise HTTPException(409, "El identificador del intento ya está en uso.")
            return {key: row[key] for key in ("id", "question_id", "version", "response", "reference")}

    def guidance(attempt_id: UUID, kind: str):
        attempt_key = str(attempt_id)
        query = "SELECT text, model FROM guidance WHERE attempt_id = ? AND kind = ?"
        parameters = (attempt_key, kind)
        with connect(db_path) as database:
            row = _row(database, ATTEMPTS, (attempt_key,), "Intento no encontrado.")
            cached = database.execute(query, parameters).fetchone()
        model = None
        outdated_model = None
        if cached:
            cached_identity = json.loads(cached["model"])
            if kind == "reinforcement":
                model = get_engine()
                expected_prompt = getattr(model, "reinforcement_prompt_version", None)
                if expected_prompt and cached_identity.get("prompt_version") != expected_prompt:
                    outdated_model = cached["model"]
            if outdated_model is None:
                return {"attempt_id": attempt_key, "text": cached["text"], "model": cached_identity, "reused": True}

        model = model if model is not None else get_engine()
        try:
            text = model.guidance(
                row["fragment"], row["question"], row["reference"], row["response"],
                practice=kind == "reinforcement",
            ).strip()
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except Exception as error:
            LOGGER.exception("El motor falló al generar apoyo")
            raise HTTPException(503, "El motor no pudo generar el apoyo. Tu intento quedó guardado.") from error
        if not text:
            raise HTTPException(502, "El motor devolvió un apoyo vacío. Tu intento quedó guardado.")
        model_identity = {**model.status(), "adapter_active": False, "adapter_version": None, "profile": None}
        if kind == "reinforcement" and getattr(model, "reinforcement_prompt_version", None):
            model_identity["prompt_version"] = model.reinforcement_prompt_version
        with connect(db_path, write=True) as database:
            cursor = database.execute(
                "INSERT INTO guidance (attempt_id, kind, text, model) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (attempt_id, kind) DO UPDATE SET text = excluded.text, model = excluded.model "
                "WHERE guidance.model = ?",
                (*parameters, text, json.dumps(model_identity), outdated_model),
            )
            saved = database.execute(query, parameters).fetchone()
            return {"attempt_id": attempt_key, "text": saved["text"], "model": json.loads(saved["model"]), "reused": cursor.rowcount == 0}

    @application.post("/api/attempts/{attempt_id}/explanation")
    def explain(attempt_id: UUID):
        return guidance(attempt_id, "explanation")

    @application.post("/api/attempts/{attempt_id}/reinforcement")
    def reinforce(attempt_id: UUID):
        return guidance(attempt_id, "reinforcement")

    return application


app = create_app(
    Path(os.environ.get("EDUCORE_DB", "data/educore.sqlite3")),
    Path(os.environ.get("EDUCORE_ADAPTER_DIR", "artifacts/main-01/adapter")),
)
