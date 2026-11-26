"""Course material, reviewed quizzes and evidence-based practice in the local demo."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from threading import Lock
from typing import Annotated, Literal
import unicodedata
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints

from . import course_store, learner_store
from .material_import import MAX_FILE_BYTES, extract_pages
from .store import connect


LOGGER = logging.getLogger(__name__)
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]
SPANISH_STOPWORDS = {
    "como", "para", "desde", "entre", "sobre", "hacia", "hasta", "mediante", "segun",
    "esta", "este", "estos", "estas", "esto", "cada", "otro", "otra", "otros", "otras",
    "puede", "pueden", "debe", "deben", "tiene", "tienen", "tener", "hacer", "seria", "seran",
    "tambien", "donde", "cuando", "porque", "cual", "cuales", "mismo", "misma", "solo",
    "distinguir", "comprender", "identificar", "conocer", "analizar", "explicar", "aplicar",
    "reconocer", "relacionar", "interpretar", "utilizar", "objetivo", "objetivos", "unidad", "curso",
}
OVERVIEW_WORDS = {"objetivos", "conclusiones", "conclusion", "contenidos", "agenda", "resumen", "perspectivas", "futuro", "aprenderemos", "aprenderas"}


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def required(database, query, parameters, message="No encontramos este elemento."):
    row = database.execute(query, parameters).fetchone()
    if row is None:
        raise HTTPException(404, message)
    return row


def normalized(text):
    return " ".join(text.split())


def content_key(text):
    """Compare rendered text while ignoring case, accents and punctuation-only changes."""
    folded = "".join(character for character in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(character))
    return " ".join(re.sub(r"[^\w\s]", " ", folded).split())


def word_tokens(text):
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))
    return set(re.findall(r"[a-z0-9]{4,}", text)) - SPANISH_STOPWORDS


class CourseInput(BaseModel):
    title: Short
    code: str = Field(default="", max_length=50)


class UnitInput(BaseModel):
    title: Short
    position: int = Field(ge=1, le=100, strict=True)
    objectives: str = Field(default="", max_length=3000)


class TextInput(BaseModel):
    title: Short
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=100, max_length=100000)]


class QuizInput(BaseModel):
    title: Short
    objective: Text
    kind: Literal["practice", "official"] = "practice"
    question_type: Literal["multiple_choice", "open", "mixed"] = "multiple_choice"
    count: int = Field(default=10, ge=5, le=30)
    material_ids: list[UUID] = Field(min_length=1, max_length=20)


class PracticeDrillInput(BaseModel):
    count: int = Field(default=10, ge=10, le=20)
    question_type: Literal["multiple_choice", "open", "mixed"] = "multiple_choice"


class Citation(BaseModel):
    material_id: UUID
    filename: str = Field(min_length=1, max_length=300)
    page_number: int = Field(ge=1, le=100, strict=True)
    label: str = Field(min_length=1, max_length=100)
    quote: Annotated[str, StringConstraints(strip_whitespace=True, min_length=12, max_length=10000)]


class Item(BaseModel):
    id: UUID
    type: Literal["multiple_choice", "open"]
    topic: Short
    objective: Text
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=1500)]
    options: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]] = Field(default_factory=list, max_length=4)
    correct_index: int | None = Field(default=None, ge=0, le=3, strict=True)
    answer: Text
    explanation: Text
    citation: Citation
    reviewed: bool = Field(default=False, strict=True)
    model: dict = Field(default_factory=dict)


class QuizEdit(BaseModel):
    version: int = Field(ge=1, strict=True)
    title: Short
    objective: Text
    items: list[Item] = Field(min_length=1, max_length=30)


class VersionInput(BaseModel):
    version: int = Field(ge=1, strict=True)


class AnswerInput(BaseModel):
    item_id: UUID
    selected_index: int | None = Field(default=None, ge=0, le=3, strict=True)
    response: str = Field(default="", max_length=5000)
    justification: str = Field(default="", max_length=3000)


class AttemptInput(VersionInput):
    request_id: UUID
    student_id: UUID
    answers: list[AnswerInput] = Field(min_length=1, max_length=30)


class ReinforcementInput(BaseModel):
    request_id: UUID
    selected_index: int | None = Field(default=None, ge=0, le=3, strict=True)
    response: str = Field(default="", max_length=5000)
    used_help: bool = Field(default=False, strict=True)


def public_item(item):
    return {key: item[key] for key in ("id", "type", "topic", "objective", "question", "options")}


def quiz_summary(quiz):
    return {**{key: value for key, value in quiz.items() if key != "items"},
            "item_count": len(quiz["items"]), "reviewed_count": sum(item["reviewed"] for item in quiz["items"])}


def public_quiz(quiz):
    return {**{key: quiz[key] for key in ("id", "unit_id", "title", "objective", "version", "kind")},
            "items": [public_item(item) for item in quiz["items"]]}


def public_reinforcement(value):
    output = {key: val for key, val in value.items() if key not in {"private_exercise", "generation_token"}}
    output["exercise"] = public_item(value["private_exercise"]) if value.get("private_exercise") else None
    return output


def restart_reinforcement_generation(value):
    """Keep the learning plan while discarding output from an unanswered run."""
    value.update(status="generating", error=None, generation_token=str(uuid4()),
                 explanation="", example="", citation=None, private_exercise=None,
                 matching=[], model={})
    for field in ("activity_kind", "support_basis", "source_review", "fallback_reason"):
        value.pop(field, None)


class CourseService:
    def __init__(self, db_path: Path, get_engine, *, get_reinforcement_engine=None):
        self.db = db_path
        self.files = db_path.parent / (db_path.stem + "-materials")
        self.get_engine = get_engine
        self.get_reinforcement_engine = get_reinforcement_engine or get_engine
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="educore-course")
        self.futures = []
        self.queue_lock = Lock()

    def initialize(self):
        course_store.initialize(self.db)
        self.files.mkdir(parents=True, exist_ok=True)
        # A stopped process cannot continue an in-memory generation job.
        with connect(self.db, write=True) as db:
            for table in ("ec_quizzes", "ec_reinforcements"):
                for row in db.execute(f"SELECT id,payload FROM {table} WHERE status='generating'").fetchall():
                    value = json.loads(row["payload"])
                    value.update(status="failed", error="La generación se interrumpió al reiniciar. Puedes reintentarlo sin perder lo guardado.")
                    db.execute(f"UPDATE {table} SET status='failed',payload=? WHERE id=?", (dump(value), row["id"]))

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def wait_for_idle(self):
        """Allow deterministic integration tests without polling or GPU calls."""
        for future in list(self.futures):
            future.result(timeout=20)

    def enqueue(self, function, identifier, *arguments):
        with self.queue_lock:
            self.futures = [future for future in self.futures if not future.done()]
            self.futures.append(self.executor.submit(function, identifier, *arguments))

    def course(self, db, identifier):
        return dict(required(db, "SELECT c.*, (SELECT COUNT(*) FROM ec_units WHERE course_id=c.id) AS unit_count FROM ec_courses c WHERE id=?", (identifier,), "Asignatura no encontrada."))

    def unit(self, db, identifier):
        return dict(required(db, "SELECT u.*, (SELECT COUNT(*) FROM ec_materials WHERE unit_id=u.id) AS material_count, (SELECT COUNT(*) FROM ec_quizzes WHERE unit_id=u.id) AS quiz_count FROM ec_units u WHERE id=?", (identifier,), "Unidad no encontrada."))

    def material(self, db, identifier):
        value = dict(required(db, "SELECT * FROM ec_materials WHERE id=?", (identifier,), "Material no encontrado."))
        return {key: val for key, val in value.items() if key != "storage_name"}

    def quiz(self, db, identifier):
        value = json.loads(required(db, "SELECT payload FROM ec_quizzes WHERE id=?", (identifier,), "Actividad no encontrada.")["payload"])
        value["published_version"] = db.execute("SELECT MAX(version) FROM ec_publications WHERE quiz_id=?", (identifier,)).fetchone()[0]
        return value

    def publication(self, db, identifier, version=None):
        if version is None:
            row = required(db, "SELECT payload FROM ec_publications WHERE quiz_id=? ORDER BY version DESC LIMIT 1", (identifier,), "Esta práctica todavía no está publicada.")
        else:
            row = required(db, "SELECT payload FROM ec_publications WHERE quiz_id=? AND version=?", (identifier, version), "La versión de la práctica no está publicada.")
        quiz = json.loads(row["payload"])
        if quiz["kind"] != "practice":
            raise HTTPException(404, "El banco oficial no está disponible en el espacio de práctica.")
        return quiz

    def attempt(self, db, identifier, student_id=None):
        row = required(db, "SELECT a.payload,a.student_id,u.course_id,s.name AS student_name FROM ec_attempts a JOIN ec_units u ON u.id=a.unit_id LEFT JOIN ec_students s ON s.id=a.student_id WHERE a.id=?", (identifier,), "Intento no encontrado.")
        learner_store.ensure_owner(db, row["student_id"], student_id)
        result = {**json.loads(row["payload"]), "student_id": row["student_id"], "student_name": row["student_name"], "course_id": row["course_id"]}
        reinforcement = db.execute("SELECT id FROM ec_reinforcements WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1", (identifier,)).fetchone()
        result["reinforcement_id"] = reinforcement["id"] if reinforcement else None
        return result

    def reinforcement(self, db, identifier, student_id=None):
        row = required(db, "SELECT r.payload,r.parent_id,a.student_id,a.unit_id,u.course_id FROM ec_reinforcements r JOIN ec_attempts a ON a.id=r.attempt_id JOIN ec_units u ON u.id=a.unit_id WHERE r.id=?", (identifier,), "Refuerzo no encontrado.")
        learner_store.ensure_owner(db, row["student_id"], student_id)
        return {**json.loads(row["payload"]), "parent_id": row["parent_id"], "student_id": row["student_id"], "unit_id": row["unit_id"], "course_id": row["course_id"]}

    def page(self, db, material_id, number):
        return dict(required(db, "SELECT p.*,m.filename FROM ec_pages p JOIN ec_materials m ON m.id=p.material_id WHERE material_id=? AND number=?", (material_id, number), "La página citada no existe en el material."))

    def validate_item(self, db, item, unit_id, material_ids, *, generated=False):
        value = Item.model_validate(item).model_dump(mode="json")
        citation = value["citation"]
        material = self.material(db, citation["material_id"])
        if material["unit_id"] != unit_id or material["id"] not in material_ids:
            raise ValueError("La fuente de la pregunta debe pertenecer a los materiales seleccionados de esta unidad.")
        page = self.page(db, material["id"], citation["page_number"])
        if normalized(citation["quote"]) not in normalized(page["text"]):
            raise ValueError("La cita de la pregunta no coincide con la página del material.")
        citation.update(filename=material["filename"], label=page["label"])
        if value["type"] == "multiple_choice":
            if len(value["options"]) != 4 or len({content_key(option) for option in value["options"]}) != 4:
                raise ValueError("Cada pregunta de selección necesita cuatro alternativas diferentes.")
            if value["correct_index"] is None:
                raise ValueError("Selecciona la alternativa correcta de cada pregunta.")
            if generated and normalized(value["answer"]) != normalized(value["options"][value["correct_index"]]):
                raise ValueError("La clave generada no coincide con su respuesta. Reintenta para obtener una propuesta consistente.")
            value["answer"] = value["options"][value["correct_index"]]
        elif value["options"] or value["correct_index"] is not None:
            raise ValueError("Una pregunta abierta no debe contener alternativas ni clave de selección.")
        return value

    def save_material(self, unit_id, filename, data, *, text=None):
        digest = hashlib.sha256(data).hexdigest()
        with connect(self.db) as db:
            self.unit(db, unit_id)
            found = db.execute("SELECT id FROM ec_materials WHERE unit_id=? AND sha256=?", (unit_id, digest)).fetchone()
            if found:
                return self.material(db, found["id"])
        kind, pages = ("text", [{"number": 1, "label": "Texto 1", "text": text}]) if text is not None else extract_pages(filename, data)
        identifier = str(uuid4())
        safe_filename = filename.replace("\\", "/").split("/")[-1].strip()[:250] or f"Material.{kind}"
        storage_name = identifier + "." + kind
        path = self.files / storage_name
        path.write_bytes(data)
        try:
            from .cloud_engines import backup_material_to_s3
            backup_material_to_s3(safe_filename, data, identifier)
        except Exception:
            LOGGER.warning("No se pudo respaldar %s en S3; se mantiene copia local.", safe_filename, exc_info=True)
        try:
            with connect(self.db, write=True) as db:
                # Concurrent repeated upload still returns the same existing material.
                found = db.execute("SELECT id FROM ec_materials WHERE unit_id=? AND sha256=?", (unit_id, digest)).fetchone()
                if found:
                    path.unlink(missing_ok=True)
                    return self.material(db, found["id"])
                db.execute("INSERT INTO ec_materials VALUES (?,?,?,?,?,?,?,?)", (identifier, unit_id, safe_filename, kind, digest, len(pages), now(), storage_name))
                db.executemany("INSERT INTO ec_pages VALUES (?,?,?,?,?)", [(str(uuid4()), identifier, page["number"], page["label"], page["text"]) for page in pages])
                return self.material(db, identifier)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def candidate_pages(self, db, quiz):
        words = word_tokens(quiz["objective"])

        def priority(page):
            heading = page["text"][:250]
            overview = bool(set(content_key(heading).split()) & OVERVIEW_WORDS)
            heading_overlap = len(words & word_tokens(heading))
            overlap = len(words & word_tokens(page["text"]))
            rich_text = len(page["text"].strip()) >= 180
            return (rich_text, not overview, overlap + 2 * heading_overlap, heading_overlap, min(len(page["text"]), 2500), -page["number"])

        groups = []
        for material_id in quiz["material_ids"]:
            rows = [dict(row) for row in db.execute("SELECT p.*,m.filename FROM ec_pages p JOIN ec_materials m ON m.id=p.material_id WHERE material_id=?", (material_id,))]
            rows = [page for page in rows if len(page["text"].strip()) > 100]
            if len(rows) > 1:
                rows = [page for page in rows if page["number"] != 1 or len(page["text"].strip()) >= 600]
            rows.sort(key=priority, reverse=True)
            if rows:
                groups.append(rows)
        if not groups:
            raise ValueError("Los materiales no tienen páginas con suficiente texto. Añade contenido seleccionable antes de generar.")
        # Interleave documents so a batch does not silently use only the first file.
        return [group[index] for index in range(max(len(group) for group in groups)) for group in groups if index < len(group)]

    def generate_quiz(self, identifier):
        try:
            with connect(self.db) as db:
                quiz = self.quiz(db, identifier)
                pages = self.candidate_pages(db, quiz)
                generation_round = db.execute("SELECT generation_round FROM ec_quizzes WHERE id=?", (identifier,)).fetchone()[0]
            engine = self.get_engine()
            stride = max(1, len(pages) // max(quiz["requested_count"], 1))
            used_pages = {
                (item["citation"]["material_id"], item["citation"]["page_number"])
                for item in quiz["items"]
                if isinstance(item.get("citation"), dict)
            }
            for index in range(len(quiz["items"]), quiz["requested_count"]):
                item_type = quiz["question_type"]
                if item_type == "mixed":
                    if quiz["requested_count"] < 3:
                        item_type = "open" if index == quiz["requested_count"] - 1 else "multiple_choice"
                    else:
                        item_type = "open" if index % 3 == 2 else "multiple_choice"
                page_index = (index * stride + generation_round * len(quiz["material_ids"])) % len(pages)
                rotated = pages[page_index:] + pages[:page_index]
                unused = [p for p in rotated if (p["material_id"], p["number"]) not in used_pages]
                reused = [p for p in rotated if (p["material_id"], p["number"]) in used_pages]
                candidates = (unused + reused)[: min(6, len(rotated))]
                prev_questions = [item["question"] for item in quiz["items"]]
                prev_answers = [item["answer"] for item in quiz["items"] if item.get("answer")]
                for candidate_index, page in enumerate(candidates):
                    try:
                        try:
                            generated = engine.generate_course_item(
                                page, quiz["objective"], item_type, prev_questions, previous_answers=prev_answers, style_index=index
                            )
                        except TypeError:
                            generated = engine.generate_course_item(page, quiz["objective"], item_type, prev_questions)
                        break
                    except ValueError:
                        if candidate_index == len(candidates) - 1:
                            raise
                        LOGGER.info("La actividad %s probará otra página para la pregunta %s", identifier, index + 1)
                generated.update(id=str(uuid4()), reviewed=False)
                with connect(self.db, write=True) as db:
                    item = self.validate_item(db, generated, quiz["unit_id"], quiz["material_ids"], generated=True)
                    if item["type"] != item_type:
                        raise ValueError("La IA no respetó el tipo de pregunta solicitado. Reintenta la generación.")
                    if any(content_key(item["question"]) == content_key(old["question"]) for old in quiz["items"]):
                        raise ValueError("La IA repitió una pregunta. Reintenta para completar el lote.")
                    quiz["items"].append(item)
                    if isinstance(item.get("citation"), dict):
                        used_pages.add((item["citation"]["material_id"], item["citation"]["page_number"]))
                    db.execute("UPDATE ec_quizzes SET payload=? WHERE id=? AND status='generating'", (dump(quiz), identifier))
            quiz.update(status="draft", error=None)
            with connect(self.db, write=True) as db:
                db.execute("UPDATE ec_quizzes SET status='draft',payload=? WHERE id=?", (dump(quiz), identifier))
        except Exception as error:
            LOGGER.exception("No se pudo completar la actividad %s", identifier)
            self.fail_job("ec_quizzes", identifier, error)

    def fail_job(self, table, identifier, error, *, generation_token=None):
        message = str(error.detail) if isinstance(error, HTTPException) else str(error) if isinstance(error, ValueError) else "El motor no pudo completar la generación. Lo guardado se conserva; puedes reintentarlo."
        with connect(self.db, write=True) as db:
            value = json.loads(required(db, f"SELECT payload FROM {table} WHERE id=?", (identifier,))["payload"])
            # A late worker must never overwrite a source review or a submitted response.
            if table == "ec_reinforcements" and (value["status"] != "generating" or value.get("response") or value.get("generation_token") != generation_token):
                return
            value.update(status="failed", error=message[:1000])
            if table == "ec_reinforcements":
                db.execute("UPDATE ec_reinforcements SET status='failed',payload=? WHERE id=? AND status='generating' AND response_request_id IS NULL AND json_extract(payload,'$.generation_token') IS ?", (dump(value), identifier, generation_token))
            else:
                db.execute(f"UPDATE {table} SET status='failed',payload=? WHERE id=?", (dump(value), identifier))

    def reviewed_source_item(self, db, value, attempt):
        """Only the immutable, teacher-reviewed target may back a source review."""
        targets = [item for item in attempt["items"] if item["id"] in value["target_item_ids"]]
        if len(targets) != 1 or targets[0].get("reviewed") is not True:
            raise ValueError("No hay una pregunta revisada disponible. Pide a tu docente que revise la actividad de esta unidad.")
        target = targets[0]
        return self.validate_item(db, target, attempt["unit_id"], [target["citation"]["material_id"]], generated=True)

    def complete_source_review(self, identifier, *, reason, generation_token=None, fallback_reason=None):
        """Finish immediately from verified class material; no model or new-case claims.

        fallback_reason explica a la interfaz por qué no hay caso nuevo:
        "motor_no_disponible" o "controles_rechazaron". Es None si lo pidió el estudiante.
        """
        with connect(self.db, write=True) as db:
            value = self.reinforcement(db, identifier)
            if value["status"] not in ("generating", "failed") or value.get("response") or value.get("generation_token") != generation_token:
                return value
            attempt = self.attempt(db, value["attempt_id"])
            target = self.reviewed_source_item(db, value, attempt)
            exercise = {**target, "id": str(uuid4()), "model": {"mode": "source_review"}}
            value.update(
                status="ready", error=None, activity_kind="source_review", topic=target["topic"],
                explanation=target["explanation"], example=target["citation"]["quote"],
                citation=target["citation"], private_exercise=exercise, matching=[],
                model={"mode": "source_review", "reason": reason},
                source_review={"source_item_id": target["id"], "quiz_id": attempt["quiz_id"], "quiz_version": attempt["version"]},
            )
            if fallback_reason:
                value["fallback_reason"] = fallback_reason
            if value.get("adaptation"):
                # Keep the original evidence, profile and planned strategy as historical facts.
                value["adaptation"]["resolved_format"] = "steps"
                value["adaptation"]["format_reason"] = "Este repaso utiliza la explicación y la referencia revisadas de la clase, paso a paso."
                value["adaptation"]["reasoning_considered"] = False
                value["adaptation"]["reasoning_note"] = "Si compartiste tu razonamiento, queda guardado para revisión. Este repaso utiliza la explicación y la referencia del material del profesor."
            db.execute("UPDATE ec_reinforcements SET status='ready',payload=? WHERE id=? AND status IN ('generating','failed') AND response_request_id IS NULL AND json_extract(payload,'$.generation_token') IS ?", (dump(value), identifier, generation_token))
            return value

    def generate_reinforcement(self, identifier, generation_token=None):
        try:
            with connect(self.db) as db:
                value = self.reinforcement(db, identifier)
                if value["status"] != "generating" or value.get("response") or value.get("generation_token") != generation_token:
                    return
                attempt = self.attempt(db, value["attempt_id"])
                reviewed = self.reviewed_source_item(db, value, attempt)
                target = [{**next(item for item in attempt["items"] if item["id"] == reviewed["id"]), **reviewed}]
                source = target[0]["citation"]
                page = self.page(db, source["material_id"], source["page_number"])
            result = self.get_reinforcement_engine().generate_course_reinforcement({**attempt, "items": target, "adaptation": value.get("adaptation")}, page)
            with connect(self.db) as db:
                exercise = self.validate_item(db, {**result["exercise"], "id": str(uuid4()), "reviewed": False}, attempt["unit_id"], [source["material_id"]], generated=True)
                if content_key(exercise["question"]) in {content_key(item["question"]) for item in attempt["items"]}:
                    raise ValueError("El refuerzo repitió una pregunta anterior. Genera un ejercicio diferente.")
                citation = Citation.model_validate(result["citation"]).model_dump(mode="json")
                cited_page = self.page(db, citation["material_id"], citation["page_number"])
                if citation["material_id"] != source["material_id"] or normalized(citation["quote"]) not in normalized(cited_page["text"]):
                    raise ValueError("La explicación del refuerzo no contiene una cita verificable de su material.")
                citation.update(filename=cited_page["filename"], label=cited_page["label"])
            clean_matching = []
            try:
                matching = result.get("matching", [])
                if not isinstance(matching, list) or len(matching) > 5 or len(matching) == 1:
                    raise ValueError("Formato de relaciones inválido")
                for index, pair in enumerate(matching):
                    if not isinstance(pair, dict) or not all(isinstance(pair.get(field), str) and 0 < len(pair[field].strip()) <= 1000 for field in ("concept", "definition")):
                        raise ValueError("Concepto vacío o inválido")
                    clean_matching.append({"id": str(index + 1), "concept": pair["concept"].strip(), "definition": pair["definition"].strip()})
                if len({pair["concept"].casefold() for pair in clean_matching}) != len(clean_matching) or len({pair["definition"].casefold() for pair in clean_matching}) != len(clean_matching):
                    raise ValueError("Pares duplicados")
            except ValueError:
                # A malformed optional template must not replace a valid exercise with invented content.
                LOGGER.warning("Se omitió una plantilla de relaciones inválida del refuerzo %s", identifier)
                clean_matching = []
            for field in ("topic", "explanation", "example"):
                if not isinstance(result.get(field), str) or not result[field].strip() or len(result[field]) > 6000:
                    raise ValueError("El apoyo generado está incompleto. Vuelve a intentarlo.")
            value.update(status="ready", error=None, activity_kind="generated_case", topic=result["topic"], explanation=result["explanation"], example=result["example"], citation=citation, private_exercise=exercise, matching=clean_matching, model=result.get("model", {}))
            value["support_basis"] = result.get("support_basis", "generated_example")
            # Presentation fallback is computed by the generation validator.
            # Keep the selection evidence and original profile snapshot intact.
            generated_adaptation = result.get("adaptation") or {}
            if value.get("adaptation"):
                for key in ("resolved_format", "format_reason", "reasoning_considered", "reasoning_note"):
                    if key in generated_adaptation:
                        value["adaptation"][key] = generated_adaptation[key]
            with connect(self.db, write=True) as db:
                db.execute("UPDATE ec_reinforcements SET status='ready',payload=? WHERE id=? AND status='generating' AND response_request_id IS NULL AND json_extract(payload,'$.generation_token') IS ?", (dump(value), identifier, generation_token))
        except Exception as error:
            LOGGER.exception("Se usará un repaso del material para el refuerzo %s", identifier)
            # ValueError o 4xx = los controles de contenido, cita o clave descartaron las propuestas.
            # Cualquier otra falla (503 al cargar, CUDA, memoria) = el motor no estuvo disponible.
            rejected = isinstance(error, ValueError) or (isinstance(error, HTTPException) and error.status_code < 500)
            fallback_reason = "controles_rechazaron" if rejected else "motor_no_disponible"
            try:
                self.complete_source_review(identifier, reason="generation_unavailable", generation_token=generation_token,
                                            fallback_reason=fallback_reason)
            except Exception as source_error:
                LOGGER.exception("No se pudo validar el material del refuerzo %s", identifier)
                self.fail_job("ec_reinforcements", identifier, source_error, generation_token=generation_token)


def build_router(service: CourseService):
    router = APIRouter(prefix="/api")
    router.include_router(learner_store.build_router(service))

    @router.get("/courses")
    def list_courses():
        with connect(service.db) as db:
            return [service.course(db, row["id"]) for row in db.execute("SELECT id FROM ec_courses ORDER BY rowid")]

    @router.post("/courses", status_code=201)
    def create_course(request: CourseInput):
        with connect(service.db, write=True) as db:
            identifier = str(uuid4())
            db.execute("INSERT INTO ec_courses VALUES (?,?,?)", (identifier, request.title, request.code.strip()))
            return service.course(db, identifier)

    @router.get("/courses/{course_id}")
    def get_course(course_id: UUID):
        with connect(service.db) as db:
            result = service.course(db, str(course_id))
            result["units"] = [service.unit(db, row["id"]) for row in db.execute("SELECT id FROM ec_units WHERE course_id=? ORDER BY position,rowid", (str(course_id),))]
            return result

    @router.post("/courses/{course_id}/units", status_code=201)
    def create_unit(course_id: UUID, request: UnitInput):
        with connect(service.db, write=True) as db:
            service.course(db, str(course_id))
            identifier = str(uuid4())
            db.execute("INSERT INTO ec_units VALUES (?,?,?,?,?)", (identifier, str(course_id), request.title, request.position, request.objectives.strip()))
            return service.unit(db, identifier)

    @router.get("/units/{unit_id}")
    def get_unit(unit_id: UUID):
        with connect(service.db) as db:
            unit = service.unit(db, str(unit_id))
            unit["course"] = service.course(db, unit["course_id"])
            unit["materials"] = [service.material(db, row["id"]) for row in db.execute("SELECT id FROM ec_materials WHERE unit_id=? ORDER BY created_at", (str(unit_id),))]
            unit["quizzes"] = [quiz_summary(service.quiz(db, row["id"])) for row in db.execute("SELECT id FROM ec_quizzes WHERE unit_id=? ORDER BY rowid DESC", (str(unit_id),))]
            return unit

    @router.post("/units/{unit_id}/materials", status_code=201)
    def upload(unit_id: UUID, file: Annotated[UploadFile, File()]):
        data = file.file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "El archivo supera el límite de 40 MB.")
        try:
            return service.save_material(str(unit_id), file.filename or "material", data)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @router.post("/units/{unit_id}/text", status_code=201)
    def upload_text(unit_id: UUID, request: TextInput):
        return service.save_material(str(unit_id), request.title + ".txt", request.text.encode("utf8"), text=request.text)

    @router.get("/materials/{material_id}/pages")
    def material_pages(material_id: UUID):
        with connect(service.db) as db:
            service.material(db, str(material_id))
            return [dict(row) for row in db.execute("SELECT p.*,m.filename FROM ec_pages p JOIN ec_materials m ON m.id=p.material_id WHERE material_id=? ORDER BY number", (str(material_id),))]

    @router.get("/materials/{material_id}/file")
    def material_file(material_id: UUID):
        with connect(service.db) as db:
            material = required(db, "SELECT * FROM ec_materials WHERE id=?", (str(material_id),), "Material no encontrado.")
            path = service.files / material["storage_name"]
            if not path.is_file():
                raise HTTPException(404, "No se encuentra la copia del archivo. El texto extraído sigue disponible.")
            mime = {"pdf": "application/pdf", "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "text": "text/plain; charset=utf-8"}[material["kind"]]
            return FileResponse(path, filename=material["filename"], media_type=mime, content_disposition_type="inline" if material["kind"] == "pdf" else "attachment")

    @router.post("/units/{unit_id}/quizzes", status_code=202)
    def create_quiz(unit_id: UUID, request: QuizInput):
        identifier = str(uuid4())
        material_ids = list(dict.fromkeys(str(value) for value in request.material_ids))
        value = {"id": identifier, "unit_id": str(unit_id), "title": request.title, "objective": request.objective, "kind": request.kind, "question_type": request.question_type, "requested_count": request.count, "status": "generating", "version": 1, "published_version": None, "items": [], "error": None, "created_at": now(), "material_ids": material_ids}
        with connect(service.db, write=True) as db:
            service.unit(db, str(unit_id))
            for material_id in material_ids:
                if service.material(db, material_id)["unit_id"] != str(unit_id):
                    raise HTTPException(422, "Elige únicamente materiales de esta unidad.")
            try:
                service.candidate_pages(db, value)
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            db.execute("INSERT INTO ec_quizzes (id,unit_id,status,version,payload) VALUES (?,?,?,?,?)", (identifier, str(unit_id), "generating", 1, dump(value)))
        service.enqueue(service.generate_quiz, identifier)
        return value

    @router.get("/quizzes/{quiz_id}")
    def get_quiz(quiz_id: UUID):
        with connect(service.db) as db:
            return service.quiz(db, str(quiz_id))

    @router.post("/quizzes/{quiz_id}/retry", status_code=202)
    def retry_quiz(quiz_id: UUID):
        with connect(service.db, write=True) as db:
            value = service.quiz(db, str(quiz_id))
            if value["status"] != "failed":
                raise HTTPException(409, "Solo se puede reintentar una generación interrumpida o fallida.")
            value.update(status="generating", error=None)
            db.execute("UPDATE ec_quizzes SET status='generating',payload=?,generation_round=generation_round+1 WHERE id=?", (dump(value), str(quiz_id)))
        service.enqueue(service.generate_quiz, str(quiz_id))
        return value

    @router.put("/quizzes/{quiz_id}")
    def edit_quiz(quiz_id: UUID, request: QuizEdit):
        with connect(service.db, write=True) as db:
            value = service.quiz(db, str(quiz_id))
            if value["status"] not in {"draft", "published"} or request.version != value["version"]:
                raise HTTPException(409, "Completa la generación y abre la versión más reciente antes de editar la actividad.")
            if {str(item.id) for item in request.items} != {item["id"] for item in value["items"]} or len(request.items) != len(value["items"]):
                raise HTTPException(422, "Conserva los identificadores de las preguntas de esta actividad.")
            try:
                items = [service.validate_item(db, item.model_dump(mode="json"), value["unit_id"], value["material_ids"]) for item in request.items]
            except ValueError as error:
                raise HTTPException(422, str(error)) from error
            original_items = {item["id"]: item for item in value["items"]}
            for item in items:
                # Provenance describes the original inference, not an editable author field.
                item["model"] = original_items[item["id"]]["model"]
            if len({content_key(item["question"]) for item in items}) != len(items):
                raise HTTPException(422, "La actividad contiene preguntas duplicadas.")
            value.update(title=request.title, objective=request.objective, items=items, version=value["version"] + 1, status="draft", error=None)
            db.execute("UPDATE ec_quizzes SET status='draft',version=?,payload=? WHERE id=?", (value["version"], dump(value), str(quiz_id)))
            return value

    @router.post("/quizzes/{quiz_id}/publish")
    def publish_quiz(quiz_id: UUID, request: VersionInput):
        with connect(service.db, write=True) as db:
            value = service.quiz(db, str(quiz_id))
            if request.version != value["version"] or value["status"] not in {"draft", "published"}:
                raise HTTPException(409, "Abre el borrador completo y más reciente antes de publicarlo.")
            if len(value["items"]) != value["requested_count"] or not all(item["reviewed"] for item in value["items"]):
                raise HTTPException(422, "Revisa todas las preguntas, sus fuentes y sus respuestas correctas antes de publicar.")
            value.update(status="published", error=None, published_version=value["version"])
            db.execute("INSERT INTO ec_publications VALUES (?,?,?) ON CONFLICT DO NOTHING", (str(quiz_id), value["version"], dump(value)))
            db.execute("UPDATE ec_quizzes SET status='published',payload=? WHERE id=?", (dump(value), str(quiz_id)))
            return value

    @router.get("/units/{unit_id}/practice")
    def list_practice(unit_id: UUID):
        with connect(service.db) as db:
            service.unit(db, str(unit_id))
            result = []
            for row in db.execute("SELECT p.payload FROM ec_publications p JOIN ec_quizzes q ON q.id=p.quiz_id WHERE q.unit_id=? AND p.version=(SELECT MAX(version) FROM ec_publications WHERE quiz_id=p.quiz_id) ORDER BY q.rowid DESC", (str(unit_id),)):
                value = json.loads(row["payload"])
                if value["kind"] == "practice":
                    value["published_version"] = value["version"]
                    result.append(quiz_summary(value))
            return result

    @router.get("/quizzes/{quiz_id}/practice")
    def get_practice(quiz_id: UUID):
        with connect(service.db) as db:
            return public_quiz(service.publication(db, str(quiz_id)))

    @router.post("/quizzes/{quiz_id}/attempts", status_code=201)
    def create_attempt(quiz_id: UUID, request: AttemptInput):
        # Idempotency belongs to a student: two profiles may safely submit the
        # same browser request UUID without sharing or overwriting a result.
        identifier = str(uuid5(request.student_id, str(request.request_id)))
        submitted = {"quiz_id": str(quiz_id), **request.model_dump(mode="json")}
        with connect(service.db, write=True) as db:
            quiz = service.publication(db, str(quiz_id), request.version)
            unit = service.unit(db, quiz["unit_id"])
            profile = learner_store.require_enrollment(db, str(request.student_id), unit["course_id"])
            found = db.execute("SELECT input FROM ec_attempts WHERE id=?", (identifier,)).fetchone()
            if found:
                if found["input"] != dump(submitted):
                    raise HTTPException(409, "Este envío ya se guardó con otra respuesta. Inicia un nuevo intento.")
                return service.attempt(db, identifier, str(request.student_id))
            answers = {str(answer.item_id): answer for answer in request.answers}
            if len(answers) != len(request.answers) or set(answers) != {item["id"] for item in quiz["items"]}:
                raise HTTPException(422, "Responde una vez cada pregunta antes de enviar la actividad.")
            items = []
            for item in quiz["items"]:
                answer = answers[item["id"]]
                if item["type"] == "multiple_choice":
                    if answer.selected_index is None or answer.selected_index >= len(item["options"]):
                        raise HTTPException(422, "Elige una alternativa en cada pregunta de selección.")
                    correct = answer.selected_index == item["correct_index"]
                    response = item["options"][answer.selected_index]
                else:
                    if answer.selected_index is not None or not answer.response.strip():
                        raise HTTPException(422, "Escribe tu respuesta en cada pregunta abierta.")
                    correct, response = None, answer.response.strip()
                items.append({**item, "selected_index": answer.selected_index, "response": response, "justification": answer.justification.strip(), "correct": correct})
            value = {"id": identifier, "quiz_id": str(quiz_id), "quiz_title": quiz["title"], "unit_id": quiz["unit_id"], "course_id": unit["course_id"], "student_id": profile["id"], "student_name": profile["name"], "profile_snapshot": {key: profile[key] for key in ("id", "name", "goals", "presentation", "updated_at")}, "version": request.version, "created_at": now(), "correct_count": sum(item["correct"] is True for item in items), "graded_count": sum(item["correct"] is not None for item in items), "open_count": sum(item["correct"] is None for item in items), "items": items, "reinforcement_id": None}
            db.execute("INSERT INTO ec_attempts (id,quiz_id,unit_id,version,submitted_at,input,payload,student_id) VALUES (?,?,?,?,?,?,?,?)", (identifier, str(quiz_id), quiz["unit_id"], request.version, value["created_at"], dump(submitted), dump(value), profile["id"]))
            return value

    @router.get("/units/{unit_id}/attempts")
    def list_attempts(unit_id: UUID, student_id: UUID | None = None):
        with connect(service.db) as db:
            unit = service.unit(db, str(unit_id))
            where, parameters = "unit_id=?", [str(unit_id)]
            if student_id is not None:
                learner_store.require_enrollment(db, str(student_id), unit["course_id"])
                where += " AND student_id=?"
                parameters.append(str(student_id))
            return [{key: val for key, val in service.attempt(db, row["id"]).items() if key not in {"items", "profile_snapshot"}} for row in db.execute("SELECT id FROM ec_attempts WHERE " + where + " ORDER BY submitted_at DESC,rowid DESC", parameters)]

    @router.get("/quiz-attempts/{attempt_id}")
    def get_attempt(attempt_id: UUID, student_id: UUID | None = None):
        with connect(service.db) as db:
            return service.attempt(db, str(attempt_id), str(student_id) if student_id else None)

    @router.post("/quiz-attempts/{attempt_id}/reinforcement", status_code=202)
    def start_reinforcement(attempt_id: UUID, student_id: UUID | None = None, source_review: bool = False):
        from .learning_profile import plan_reinforcement
        use_source = source_review
        source_reason = "student_requested"
        with connect(service.db, write=True) as db:
            scope = str(student_id) if student_id else None
            attempt = service.attempt(db, str(attempt_id), scope)
            learner_store.ensure_owner(db, attempt["student_id"], scope, write=True)
            existing = db.execute("SELECT id,payload FROM ec_reinforcements WHERE attempt_id=? ORDER BY rowid DESC LIMIT 1", (str(attempt_id),)).fetchone()
            if existing:
                value = json.loads(existing["payload"])
                if value["status"] == "ready" or value.get("response") or (value["status"] == "generating" and not source_review):
                    return public_reinforcement(value)
                if value["status"] == "failed":
                    use_source = True
                    source_reason = "recovered_failure"
                restart_reinforcement_generation(value)
                db.execute("UPDATE ec_reinforcements SET status='generating',payload=? WHERE id=?", (dump(value), value["id"]))
            else:
                profile = learner_store.require_enrollment(db, attempt["student_id"], attempt["course_id"])
                history, supports = learner_store.histories(db, profile["id"], attempt["course_id"])
                adaptation = plan_reinforcement(profile, attempt, history, supports)
                if adaptation["target_item_id"] not in {item["id"] for item in attempt["items"]}:
                    raise HTTPException(422, "El apoyo debe partir de una pregunta de este intento.")
                targets = [adaptation["target_item_id"]]
                value = {"id": str(uuid4()), "attempt_id": str(attempt_id), "student_id": profile["id"], "unit_id": attempt["unit_id"], "course_id": attempt["course_id"], "created_at": now(), "adaptation": adaptation, "status": "generating", "error": None, "target_item_ids": targets, "topic": adaptation["topic"], "explanation": "", "example": "", "citation": None, "private_exercise": None, "matching": [], "response": None, "model": {}}
                value["generation_token"] = str(uuid4())
                db.execute("INSERT INTO ec_reinforcements (id,attempt_id,status,payload) VALUES (?,?,'generating',?)", (value["id"], str(attempt_id), dump(value)))
        if use_source:
            try:
                value = service.complete_source_review(value["id"], reason=source_reason, generation_token=value.get("generation_token"))
            except Exception as source_error:
                service.fail_job("ec_reinforcements", value["id"], source_error, generation_token=value.get("generation_token"))
                with connect(service.db) as db:
                    value = service.reinforcement(db, value["id"])
        else:
            service.enqueue(service.generate_reinforcement, value["id"], value["generation_token"])
        return public_reinforcement(value)

    @router.get("/quiz-attempts/{attempt_id}/reinforcements")
    def list_reinforcement_rounds(attempt_id: UUID, student_id: UUID | None = None):
        with connect(service.db) as db:
            scope = str(student_id) if student_id else None
            service.attempt(db, str(attempt_id), scope)
            return [public_reinforcement(service.reinforcement(db, row["id"], scope)) for row in db.execute(
                "SELECT id FROM ec_reinforcements WHERE attempt_id=? ORDER BY rowid", (str(attempt_id),))]

    @router.post("/reinforcements/{reinforcement_id}/next", status_code=202)
    def next_reinforcement(reinforcement_id: UUID, student_id: UUID | None = None):
        from .learning_profile import plan_reinforcement
        with connect(service.db, write=True) as db:
            scope = str(student_id) if student_id else None
            parent = service.reinforcement(db, str(reinforcement_id), scope)
            learner_store.ensure_owner(db, parent["student_id"], scope, write=True)
            if parent["status"] != "ready" or (not parent.get("response") and parent.get("activity_kind") != "source_review"):
                raise HTTPException(409, "Completa este ejercicio antes de preparar un caso nuevo.")
            existing = db.execute("SELECT id FROM ec_reinforcements WHERE parent_id=?", (str(reinforcement_id),)).fetchone()
            if existing:
                value = service.reinforcement(db, existing["id"], scope)
                if value["status"] != "failed" or value.get("response"):
                    return public_reinforcement(value)
                restart_reinforcement_generation(value)
                db.execute("UPDATE ec_reinforcements SET status='generating',payload=? WHERE id=?", (dump(value), value["id"]))
            else:
                attempt = service.attempt(db, parent["attempt_id"], scope)
                profile = learner_store.require_enrollment(db, parent["student_id"], attempt["course_id"])
                history, supports = learner_store.histories(db, profile["id"], attempt["course_id"])
                targets = parent.get("target_item_ids", [])
                if len(targets) != 1:
                    raise HTTPException(422, "El caso nuevo necesita una pregunta de origen identificada.")
                adaptation = plan_reinforcement(profile, attempt, history, supports, target_item_id=targets[0])
                if parent.get("response"):
                    adaptation["latest_response"] = {
                        "reinforcement_id": parent["id"],
                        "question": (parent.get("private_exercise") or {}).get("question", ""),
                        "response": parent["response"].get("response", ""),
                        "correct": parent["response"].get("correct"),
                        "used_help": parent["response"].get("used_help"),
                        "activity_kind": parent.get("activity_kind", "generated_case"),
                    }
                value = {"id": str(uuid4()), "attempt_id": attempt["id"], "parent_id": parent["id"], "round_number": parent.get("round_number", 1) + 1,
                         "student_id": profile["id"], "unit_id": attempt["unit_id"], "course_id": attempt["course_id"], "created_at": now(),
                         "adaptation": adaptation, "status": "generating", "error": None, "target_item_ids": list(targets), "topic": adaptation["topic"],
                         "explanation": "", "example": "", "citation": None, "private_exercise": None, "matching": [], "response": None, "model": {}}
                value["generation_token"] = str(uuid4())
                db.execute("INSERT INTO ec_reinforcements (id,attempt_id,status,payload,parent_id) VALUES (?,?,'generating',?,?)", (value["id"], attempt["id"], dump(value), parent["id"]))
        service.enqueue(service.generate_reinforcement, value["id"], value["generation_token"])
        return public_reinforcement(value)

    @router.get("/reinforcements/{reinforcement_id}")
    def get_reinforcement(reinforcement_id: UUID, student_id: UUID | None = None):
        with connect(service.db) as db:
            return public_reinforcement(service.reinforcement(db, str(reinforcement_id), str(student_id) if student_id else None))

    @router.post("/reinforcements/{reinforcement_id}/response")
    def submit_reinforcement(reinforcement_id: UUID, request: ReinforcementInput, student_id: UUID | None = None):
        with connect(service.db, write=True) as db:
            row = required(db, "SELECT * FROM ec_reinforcements WHERE id=?", (str(reinforcement_id),), "Refuerzo no encontrado.")
            scope = str(student_id) if student_id else None
            value = service.reinforcement(db, str(reinforcement_id), scope)
            learner_store.ensure_owner(db, value["student_id"], scope, write=True)
            response_key = str(uuid5(UUID(value["student_id"]), str(request.request_id)))
            submission = dump(request.model_dump(mode="json"))
            if row["response_request_id"]:
                if row["response_request_id"] != response_key or row["response_input"] != submission:
                    raise HTTPException(409, "El intento de este ejercicio ya está guardado. Puedes revisarlo en el historial.")
                return public_reinforcement(value)
            if db.execute("SELECT id FROM ec_reinforcements WHERE response_request_id=?", (response_key,)).fetchone():
                raise HTTPException(409, "Este identificador de envío ya pertenece a otro ejercicio.")
            if value["status"] != "ready" or not value["private_exercise"]:
                raise HTTPException(409, "Espera a que el ejercicio termine de generarse.")
            exercise = value["private_exercise"]
            if exercise["type"] == "multiple_choice":
                if request.selected_index is None or request.selected_index >= len(exercise["options"]):
                    raise HTTPException(422, "Elige una alternativa antes de enviar.")
                correct = request.selected_index == exercise["correct_index"]
                response = exercise["options"][request.selected_index]
            else:
                if request.selected_index is not None or not request.response.strip():
                    raise HTTPException(422, "Escribe una respuesta antes de enviar.")
                correct, response = None, request.response.strip()
            value["response"] = {"selected_index": request.selected_index, "response": response, "correct": correct, "reference": exercise["answer"], "explanation": exercise["explanation"], "created_at": now(), "used_help": request.used_help, "activity_kind": value.get("activity_kind", "generated_case")}
            db.execute("UPDATE ec_reinforcements SET payload=?,response_request_id=?,response_input=? WHERE id=?", (dump(value), response_key, submission, str(reinforcement_id)))
            return public_reinforcement(value)

    @router.post("/reinforcements/{reinforcement_id}/practice-drill")
    def generate_reinforcement_drill(reinforcement_id: UUID, request: PracticeDrillInput, student_id: UUID | None = None):
        scope = str(student_id) if student_id else None
        with connect(service.db) as db:
            value = service.reinforcement(db, str(reinforcement_id), scope)
            learner_store.ensure_owner(db, value["student_id"], scope)
            existing = value.get("practice_drill")
            if isinstance(existing, dict) and existing.get("question_type") == request.question_type and len(existing.get("items") or []) >= request.count:
                return existing
            material_rows = db.execute("SELECT id FROM ec_materials WHERE unit_id=? ORDER BY created_at", (value["unit_id"],)).fetchall()
            material_ids = [row["id"] for row in material_rows]
            if not material_ids:
                raise HTTPException(422, "La unidad no tiene materiales disponibles para ampliar el refuerzo.")
            unit = service.unit(db, value["unit_id"])
            pages = service.candidate_pages(db, {"objective": value.get("topic") or unit.get("objectives") or "", "material_ids": material_ids})

        engine = service.get_engine()
        same_type = isinstance(existing, dict) and existing.get("question_type") == request.question_type
        items: list[dict] = list(existing.get("items") or []) if same_type else []
        used_pages = {
            (it["citation"]["material_id"], it["citation"]["page_number"])
            for it in items if isinstance(it.get("citation"), dict)
        }
        stride = max(1, len(pages) // max(request.count, 1))
        for idx in range(len(items), request.count):
            drill_type = request.question_type
            if drill_type == "mixed":
                drill_type = "open" if idx % 3 == 2 else "multiple_choice"
            page_index = (idx * stride) % len(pages)
            rotated = pages[page_index:] + pages[:page_index]
            unused = [p for p in rotated if (p["material_id"], p["number"]) not in used_pages]
            reused = [p for p in rotated if (p["material_id"], p["number"]) in used_pages]
            candidates = (unused + reused)[: min(6, len(rotated))]
            prev_q = [it["question"] for it in items]
            prev_a = [it["answer"] for it in items if it.get("answer")]
            generated = None
            for c_idx, page in enumerate(candidates):
                try:
                    try:
                        generated = engine.generate_course_item(
                            page, value.get("topic") or unit.get("objectives") or "", drill_type, prev_q, previous_answers=prev_a, style_index=idx
                        )
                    except TypeError:
                        generated = engine.generate_course_item(page, value.get("topic") or "", drill_type, prev_q)
                    break
                except ValueError:
                    if c_idx == len(candidates) - 1:
                        break
            if generated is None:
                break
            generated.update(id=str(uuid4()), reviewed=True)
            with connect(service.db) as db:
                validated = service.validate_item(db, generated, value["unit_id"], material_ids, generated=True)
            if any(content_key(validated["question"]) == content_key(old["question"]) for old in items):
                continue
            items.append(validated)
            if isinstance(validated.get("citation"), dict):
                used_pages.add((validated["citation"]["material_id"], validated["citation"]["page_number"]))

        if not items:
            raise HTTPException(502, "No se pudieron generar preguntas de práctica para este refuerzo.")
        drill = {"topic": value.get("topic") or "Refuerzo", "question_type": request.question_type, "requested_count": request.count, "items": items, "created_at": now()}
        with connect(service.db, write=True) as db:
            current = service.reinforcement(db, str(reinforcement_id), scope)
            current["practice_drill"] = drill
            db.execute("UPDATE ec_reinforcements SET payload=? WHERE id=?", (dump(current), str(reinforcement_id)))
        return drill

    return router
