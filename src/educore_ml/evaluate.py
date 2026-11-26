"""Comparación emparejada de generación, con auditoría y revisión humana ciega."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import platform
import random
import re
import shutil
import statistics
import sys
import time
import unicodedata
import uuid
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import QuestionEngine


MODES = ("base", "adapter")
REVIEW_FIELDS = (
    "claridad",
    "respaldo_en_texto",
    "correspondencia_con_respuesta",
    "ausencia_de_ambiguedad",
    "utilidad_educativa",
    "observaciones",
)


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFC", text).casefold())


def rouge_l(reference: str, candidate: str) -> float:
    """F1 de subsecuencia común más larga sobre palabras Unicode."""
    reference_words, candidate_words = words(reference), words(candidate)
    if not reference_words or not candidate_words:
        return 0.0
    previous = [0] * (len(candidate_words) + 1)
    for reference_word in reference_words:
        current = [0]
        for index, candidate_word in enumerate(candidate_words, start=1):
            if reference_word == candidate_word:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return 2 * previous[-1] / (len(reference_words) + len(candidate_words))


def output_flags(question: str) -> dict:
    stripped = question.strip()
    tokens = words(stripped)
    fourgrams = Counter(zip(tokens, tokens[1:], tokens[2:], tokens[3:]))
    invalid_reasons = []
    if stripped:
        if len(stripped.splitlines()) != 1:
            invalid_reasons.append("multilinea")
        if (
            not stripped.startswith("¿")
            or not stripped.endswith("?")
            or stripped.count("¿") != 1
            or stripped.count("?") != 1
        ):
            invalid_reasons.append("formato_interrogativo")
        if not tokens:
            invalid_reasons.append("sin_palabras")
    return {
        "empty": not stripped,
        "invalid": bool(invalid_reasons),
        "invalid_reasons": invalid_reasons,
        "repetitive": any(count > 1 for count in fourgrams.values()),
    }


def evaluation_rows(path: Path) -> list[dict]:
    rows = []
    ids = set()
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key].strip()
                for key in ("id", "context", "answer", "question")
            ):
                raise ValueError(f"{path.name}, fila {line_number}: faltan campos de evaluación")
            start = row.get("answer_start")
            if type(start) is not int or start < 0 or (
                row["context"][start : start + len(row["answer"])] != row["answer"]
            ):
                raise ValueError(f"{path.name}, fila {line_number}: respuesta fuera de su fragmento")
            if row["id"] in ids:
                raise ValueError(f"{path.name}, fila {line_number}: ID duplicado")
            ids.add(row["id"])
            rows.append(row)
    if not rows:
        raise ValueError(f"{path.name}: no contiene ejemplos")
    return rows



def distinct_document_sample(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """Selecciona como máximo una fila por documento con un orden hash fijo."""
    if limit < 1:
        raise ValueError("El límite de evaluación debe ser positivo")

    def rank(kind: str, key: str) -> bytes:
        return hashlib.sha256(f"{seed}:{kind}:{key}".encode("utf-8")).digest()

    by_document: dict[str, list[dict]] = {}
    for row in rows:
        document = row.get("document_id")
        if not isinstance(document, str) or not document:
            raise ValueError("La muestra SQAC requiere document_id en cada fila")
        by_document.setdefault(document, []).append(row)
    selected = sorted(by_document, key=lambda document: rank("documento", document))[:limit]
    return [min(by_document[document], key=lambda row: rank("pregunta", row["id"])) for document in selected]


def _compare(
    engine: QuestionEngine, rows: list[dict], max_new_tokens: int, seed: int, *, label: str = "",
) -> list[dict]:
    # Se calientan ambos modos, sin sumar estas llamadas a métricas ni latencia.
    first = rows[0]
    for mode in MODES:
        engine.generate(
            first["context"], first["answer"],
            max_new_tokens=max_new_tokens, use_adapter=mode == "adapter",
            answer_start=first["answer_start"],
        )

    results = []
    for index, row in enumerate(rows):
        result = {key: row[key] for key in ("id", "context", "answer", "answer_start")}
        result["document_id"] = row.get("document_id")
        result["reference_question"] = row["question"]
        # Alternar el orden evita que un modo siempre ejecute primero.
        order = MODES if (index + seed) % 2 == 0 else MODES[::-1]
        for mode in order:
            start = time.perf_counter()
            question = engine.generate(
                row["context"], row["answer"],
                max_new_tokens=max_new_tokens, use_adapter=mode == "adapter",
                answer_start=row["answer_start"],
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            result[mode] = {
                "question": question,
                "latency_ms": elapsed_ms,
                "rouge_l_f1": rouge_l(row["question"], question),
                **output_flags(question),
            }
        results.append(result)
        if label and ((index + 1) % 12 == 0 or index + 1 == len(rows)):
            print(f"Evaluación {label}: {index + 1}/{len(rows)} pares", file=sys.stderr, flush=True)
    return results


def reveals_answer(question: str, answer: str) -> bool:
    """La pregunta contiene literalmente la respuesta objetivo (palabras completas)."""
    answer_words, question_words = words(answer), words(question)
    size = len(answer_words)
    if not size or size > len(question_words):
        return False
    return any(
        question_words[index : index + size] == answer_words
        for index in range(len(question_words) - size + 1)
    )


def sign_test_p_value(wins: int, losses: int) -> float | None:
    """Prueba de signos exacta, bilateral, ignorando empates."""
    total = wins + losses
    if total == 0:
        return None
    tail = sum(math.comb(total, k) for k in range(min(wins, losses) + 1)) / 2**total
    return min(1.0, 2 * tail)


def _summary(pairs: list[dict]) -> dict:
    metrics = {}
    for mode in MODES:
        variants = [pair[mode] for pair in pairs]
        latencies = sorted(variant["latency_ms"] for variant in variants)
        metrics[mode] = {
            "n": len(variants),
            "rouge_l_f1_mean": statistics.fmean(variant["rouge_l_f1"] for variant in variants),
            "empty": sum(variant["empty"] for variant in variants),
            "invalid": sum(variant["invalid"] for variant in variants),
            "repetitive": sum(variant["repetitive"] for variant in variants),
            "answer_revealed": sum(
                reveals_answer(pair[mode]["question"], pair["answer"]) for pair in pairs
            ),
            "words_mean": statistics.fmean(len(words(variant["question"])) for variant in variants),
            "latency_ms_p50": statistics.median(latencies),
            "latency_ms_p95": latencies[math.ceil(len(latencies) * 0.95) - 1],
        }
    deltas = [pair["adapter"]["rouge_l_f1"] - pair["base"]["rouge_l_f1"] for pair in pairs]
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    return {
        **metrics,
        "reference_words_mean": statistics.fmean(
            len(words(pair["reference_question"])) for pair in pairs
        ),
        "rouge_l_f1_adapter_minus_base": statistics.fmean(deltas),
        "pairs_adapter_better": wins,
        "pairs_base_better": losses,
        "pairs_tied": len(deltas) - wins - losses,
        "sign_test_p_two_sided": sign_test_p_value(wins, losses),
    }


def blind_review(pairs: list[dict], count: int, seed: int) -> tuple[list[dict], list[dict]]:
    if count < 1:
        raise ValueError("La muestra de revisión debe incluir al menos un par")
    rng = random.Random(seed)
    sample = rng.sample(sorted(pairs, key=lambda pair: pair["id"]), k=min(count, len(pairs)))
    sheet, key = [], []
    # Alternancia inicial aleatoria para mantener un orden A/B equilibrado.
    first_order = rng.choice((MODES, MODES[::-1]))
    for index, pair in enumerate(sample, start=1):
        order = first_order if index % 2 else first_order[::-1]
        for label, mode in zip(("A", "B"), order, strict=True):
            code = f"R{index:03d}-{label}"
            sheet.append({
                "codigo": code,
                "contexto": pair["context"],
                "respuesta_objetivo": pair["answer"],
                "pregunta": pair[mode]["question"],
                **dict.fromkeys(REVIEW_FIELDS, ""),
            })
            key.append({"codigo": code, "id_ejemplo": pair["id"], "variante": mode})
    return sheet, key


def _spreadsheet_value(value: object) -> object:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows({key: _spreadsheet_value(value) for key, value in row.items()} for row in rows)


def _write_suite(
    path: Path, results: list[dict], review_pairs: int, seed: int,
) -> dict:
    path.mkdir()
    with (path / "predictions.jsonl").open("w", encoding="utf-8") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    _csv(path / "comparacion.csv", [{
        "id": pair["id"],
        "id_documento": pair["document_id"],
        "contexto": pair["context"],
        "respuesta_objetivo": pair["answer"],
        "pregunta_referencia": pair["reference_question"],
        "pregunta_base": pair["base"]["question"],
        "pregunta_ajustada": pair["adapter"]["question"],
        "rouge_l_f1_base": pair["base"]["rouge_l_f1"],
        "rouge_l_f1_ajustada": pair["adapter"]["rouge_l_f1"],
        "delta_rouge_l_f1": pair["adapter"]["rouge_l_f1"] - pair["base"]["rouge_l_f1"],
    } for pair in results])
    metrics = _summary(results)
    _csv(path / "metricas.csv", [{"variante": mode, **metrics[mode]} for mode in MODES])
    review, key = blind_review(results, review_pairs, seed)
    _csv(path / "revision_ciega.csv", review)
    _csv(path / "clave_revision.csv", key)
    (path / "revision_instrucciones.md").write_text(
        "# Revisión humana\n\n"
        "Comparte únicamente la hoja `revision_ciega.csv` con quien revisa. "
        "Registra cada criterio de 1 a 3 (1 insuficiente, 2 parcial, 3 logrado) "
        "y anota el motivo cuando falte respaldo, haya más de una respuesta "
        "plausible o la pregunta revele la respuesta. "
        "Ausencia de ambigüedad se puntúa con 3 si la pregunta es inequívoca. "
        "Las filas se conservan aunque la pregunta esté vacía. "
        "Abre la clave después de cerrar la revisión. "
        "Las celdas de evaluación se entregan vacías.\n",
        encoding="utf-8",
    )
    return metrics


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evaluate(
    run_dir: Path,
    data_path: Path,
    output_dir: Path,
    *,
    split: str,
    academic_path: Path | None = None,
    review_pairs: int = 12,
    seed: int = 42,
    max_new_tokens: int = 64,
    limit: int | None = None,
    precision: str = "auto",
) -> dict:
    """Evalúa cada ejemplo con el mismo modelo residente y activa/desactiva LoRA.

    ``precision="auto"`` usa BF16 si la GPU lo soporta de forma nativa y FP16 en otro caso
    (por ejemplo, una T4 de Colab). La precisión del entrenamiento queda registrada aparte.
    """
    if split not in {"validation", "test"}:
        raise ValueError("Partición de evaluación desconocida")
    if review_pairs < 1 or max_new_tokens < 1:
        raise ValueError("La muestra humana y el máximo de tokens deben ser positivos")
    if output_dir.exists():
        raise FileExistsError(f"La evaluación ya existe: {output_dir}")

    public_rows = evaluation_rows(data_path)
    available_public_rows = len(public_rows)
    if limit is not None:
        public_rows = distinct_document_sample(public_rows, limit, seed)
    public_sha256 = _sha256(data_path)
    academic_rows = evaluation_rows(academic_path) if academic_path is not None else None
    if academic_rows:
        public_contexts = {row["context"] for row in public_rows}
        if any(row["context"] in public_contexts for row in academic_rows):
            raise ValueError("La muestra académica comparte contextos con SQAC")

    import torch

    from .model import QuestionEngine

    adapter_dir = run_dir / "adapter"
    adapter_sha256 = _sha256(adapter_dir / "adapter_model.safetensors")
    metadata = json.loads((adapter_dir / "metadata.json").read_text(encoding="utf-8"))
    if split == "test" and metadata["profile"] != "train":
        raise ValueError("El test se reserva para la corrida principal del perfil train")
    engine = QuestionEngine(
        adapter_dir, precision=precision, max_length=metadata["max_length"],
    )
    status = engine.status()
    if not status["adapter_active"]:
        raise RuntimeError("La comparación requiere un adaptador activo")

    info = {
        "split": split,
        "seed": seed,
        "training_precision": metadata.get("precision"),
        "evaluation_precision": status.get("precision"),
        "review_pairs_requested_per_suite": review_pairs,
        "engine": status,
        "conditions": {
            "system": platform.platform(),
            "python": platform.python_version(),
            "torch": str(torch.__version__),
            "gpu": torch.cuda.get_device_name() if torch.cuda.is_available() else None,
            "cuda": torch.version.cuda,
            "batch_size": 1,
            "warmup_per_mode_and_suite": 1,
            "timing": "wall clock de generate, incluye preparación de entrada y decodificación",
            "generation": {"do_sample": False, "max_new_tokens": max_new_tokens},
            "rouge_l": "F1 macro, subsecuencia común de palabras Unicode sin stemming",
            "invalid": "salida no vacía sin formato de una única pregunta o sin palabras",
            "repetitive": "repite alguna secuencia de cuatro palabras",
        },
        "adapter_sha256": adapter_sha256,
        "evaluation_source_sha256": _sha256(Path(__file__)),
        "run_metrics_sha256": _sha256(run_dir / "metrics.json"),
        "suites": {
            "sqac": {
                "file": data_path.name,
                "sha256": public_sha256,
                "available_rows": available_public_rows,
                "requested_limit": limit,
                "selection": "SHA-256 con semilla, una pregunta por documento" if limit else "todas las filas",
                "selected_examples": [
                    {"id": row["id"], "document_id": row.get("document_id")} for row in public_rows
                ],
            },
        },
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Directorio temporal creado con Path.mkdir y permisos heredados. tempfile usa modo 0o700,
    # que en Windows (Python >= 3.12.4) deja una ACL solo para el creador y la conserva al
    # renombrar: otra cuenta no podría leer los resultados.
    staging_root = output_dir.parent / f".evaluacion-{uuid.uuid4().hex}"
    staging_root.mkdir()
    try:
        stage = staging_root / "resultados"
        stage.mkdir()
        results = _compare(engine, public_rows, max_new_tokens, seed, label=f"SQAC {split}")
        info["suites"]["sqac"]["metrics"] = _write_suite(
            stage / "sqac", results, review_pairs, seed,
        )
        if academic_rows and academic_path is not None:
            academic_results = _compare(engine, academic_rows, max_new_tokens, seed, label="académica")
            info["suites"]["academic"] = {
                "file": academic_path.name,
                "sha256": _sha256(academic_path),
                "metrics": _write_suite(stage / "academic", academic_results, review_pairs, seed),
            }
        (stage / "metrics.json").write_text(
            json.dumps(info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        stage.rename(output_dir)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return info
