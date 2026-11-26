"""Transparent, source-bound adaptation from an individual student's evidence.

This module does not diagnose ability or learning styles. Presentation is editable;
practice answers and supported/unsupported checks remain separate observations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import unicodedata

POLICY_VERSION = "individual-evidence-v2"


def _key(value: str) -> str:
    plain = "".join(c for c in unicodedata.normalize("NFKD", str(value).casefold()) if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", plain).split())


def _time(value: str | None) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _owned(profile: dict, attempts: list[dict], reinforcements: list[dict]):
    student_id = str(profile.get("id") or "")
    if not student_id:
        raise ValueError("Se necesita un perfil de estudiante para consultar su evidencia.")
    by_id = {str(a["id"]): a for a in attempts if a.get("id") and a.get("student_id") == student_id}
    ordered = sorted(by_id.values(), key=lambda a: (_time(a.get("created_at")), str(a["id"])))
    supports = {str(r["id"]): r for r in reinforcements if r.get("id") and r.get("student_id") == student_id and str(r.get("attempt_id")) in by_id}
    return ordered, sorted(supports.values(), key=lambda r: (_time((r.get("response") or {}).get("created_at") or r.get("created_at")), str(r["id"])))


def _topic_key(unit_id: str, item: dict):
    return str(unit_id), _key(item.get("topic") or "Contenido de la unidad")


def _strategy(events: list[dict], open_count: int) -> tuple[str, str, str, int]:
    """Use a recent sequence rather than treating all old errors as permanent."""
    scored = [e for e in events if type(e.get("correct")) is bool]
    if not scored:
        return "check", "unscored", "Las respuestas abiertas están registradas sin puntuación automática. Un caso adicional permitirá comparar las ideas con la referencia.", 0
    # A recorded independent check can close an earlier episode of difficulty.
    last_independent = max((e["time"] for e in scored if e["kind"] == "reinforcement" and e.get("activity_kind") != "source_review" and e["correct"] and e.get("used_help") is False), default=-1)
    unresolved = [e for e in scored if not e["correct"] and e["time"] > last_independent]
    wrong_attempts = {e["attempt_id"] for e in unresolved if e["kind"] == "practice"}
    last = scored[-1]
    recent = [e for e in scored if e.get("activity_kind") != "source_review"][-3:]
    # A later correct practice is evidence of progress, not proof of mastery.
    recent_clean = len(recent) >= 2 and all(e["correct"] for e in recent) and all(e.get("used_help") is not True for e in recent)
    if last.get("activity_kind") == "source_review" and last["correct"]:
        return "check", "checking", "Completaste correctamente el repaso de una pregunta ya vista. Falta comprobar la idea en otra situación; los errores anteriores siguen como referencia.", len(unresolved)
    if last["kind"] == "reinforcement" and last["correct"] and last.get("used_help") is False:
        return "transfer", "progressing", "La última comprobación fue correcta sin volver a consultar el apoyo. Proponemos otra situación para comprobar si puedes aplicar la misma idea.", len(unresolved)
    if last["correct"] and recent_clean:
        return "transfer", "progressing", "Hay respuestas correctas recientes en este tema. Cambiaremos el contexto para seguir comprobando la aplicación de la idea; esto no declara dominio completo.", len(unresolved)
    if not last["correct"] and (len(wrong_attempts) >= 2 or last["kind"] == "reinforcement"):
        return "guided", "needs_support", "El error vuelve a aparecer en intentos de este tema o en la comprobación posterior. Empezaremos con pasos breves y una comparación con la referencia revisada.", len(unresolved)
    if last["kind"] == "reinforcement" and last["correct"] and last.get("used_help") is True:
        return "check", "checking", "La respuesta posterior fue correcta con apoyo. Ahora conviene comprobar la misma idea en otro caso y registrar si necesitas consultar la ayuda.", len(unresolved)
    if last["kind"] == "reinforcement" and last["correct"] and last.get("used_help") is None:
        return "check", "checking", "La respuesta posterior fue correcta, pero no se registró el uso de ayuda. Necesitamos otra comprobación antes de cambiar el apoyo.", len(unresolved)
    if not last["correct"]:
        return "check", "checking", "Tenemos un error observado y todavía poca evidencia para explicar su causa. Revisaremos un ejemplo y haremos una pregunta adicional, sin atribuir un diagnóstico.", len(unresolved)
    return "check", "checking", "Una respuesta correcta aporta evidencia inicial. Otro caso ayudará a comprobar la aplicación de esta idea sin dar por dominado el tema.", len(unresolved)


def summarize_progress(profile: dict, attempts: list[dict], reinforcements: list[dict]) -> dict:
    """Return observations for this student only; caller can prefilter course/unit.

    Unassigned legacy attempts and records belonging to a different student are
    excluded even when a caller accidentally supplies them.
    """
    owned_attempts, supports = _owned(profile, attempts, reinforcements)
    groups: dict[tuple[str, str], dict] = {}
    item_groups = {}
    attempts_by_id = {str(a["id"]): a for a in owned_attempts}
    timeline = []
    for attempt in owned_attempts:
        correct = graded = open_count = 0
        for item in attempt.get("items", []):
            topic_key = _topic_key(attempt.get("unit_id", ""), item)
            group = groups.setdefault(topic_key, {"unit_id": topic_key[0], "topic": item.get("topic") or "Contenido de la unidad", "objectives": [], "attempt_ids": set(), "events": [], "support_ids": set(), "correct_count": 0, "incorrect_count": 0, "open_count": 0, "independently_correct_support_count": 0, "assisted_correct_support_count": 0, "completed_source_review_count": 0, "correct_source_review_count": 0})
            group["attempt_ids"].add(str(attempt["id"]))
            objective = str(item.get("objective") or "").strip()
            if objective and objective not in group["objectives"]:
                group["objectives"].append(objective)
            item_groups[(str(attempt["id"]), str(item.get("id")))] = topic_key
            outcome = item.get("correct") if item.get("type") == "multiple_choice" and type(item.get("correct")) is bool else None
            if outcome is None:
                group["open_count"] += 1
                open_count += 1
            else:
                group["correct_count" if outcome else "incorrect_count"] += 1
                graded += 1
                correct += int(outcome)
            group["events"].append({"kind": "practice", "time": _time(attempt.get("created_at")), "created_at": attempt.get("created_at"), "attempt_id": str(attempt["id"]), "correct": outcome})
        timeline.append({"id": str(attempt["id"]), "type": "practice", "attempt_id": str(attempt["id"]), "unit_id": attempt.get("unit_id"), "title": attempt.get("quiz_title") or "Práctica", "created_at": attempt.get("created_at"), "correct_count": correct, "graded_count": graded, "open_count": open_count})
    completed = independent = assisted = completed_reviews = correct_reviews = 0
    for support in supports:
        attempt_id = str(support["attempt_id"])
        target_keys = {item_groups[(attempt_id, str(target))] for target in support.get("target_item_ids", []) if (attempt_id, str(target)) in item_groups}
        response = support.get("response")
        activity_kind = support.get("activity_kind", "generated_case")
        is_review = activity_kind == "source_review"
        for topic_key in target_keys:
            group = groups[topic_key]
            group["support_ids"].add(str(support["id"]))
            if response:
                outcome = response.get("correct") if type(response.get("correct")) is bool else None
                used_help = response.get("used_help") if type(response.get("used_help")) is bool else None
                group["independently_correct_support_count"] += int(outcome is True and used_help is False and not is_review)
                group["assisted_correct_support_count"] += int(outcome is True and used_help is True and not is_review)
                group["completed_source_review_count"] += int(is_review)
                group["correct_source_review_count"] += int(is_review and outcome is True)
                group["events"].append({"kind": "reinforcement", "activity_kind": activity_kind, "time": _time(response.get("created_at")), "created_at": response.get("created_at"), "attempt_id": attempt_id, "correct": outcome, "used_help": used_help})
        if response:
            completed += 1
            completed_reviews += int(is_review)
            correct_reviews += int(is_review and response.get("correct") is True)
            outcome = response.get("correct") if type(response.get("correct")) is bool else None
            used_help = response.get("used_help") if type(response.get("used_help")) is bool else None
            independent += int(outcome is True and used_help is False and not is_review)
            assisted += int(outcome is True and used_help is True and not is_review)
            timeline.append({"id": str(support["id"]), "type": "reinforcement", "attempt_id": attempt_id, "reinforcement_id": str(support["id"]), "unit_id": attempts_by_id[attempt_id].get("unit_id"), "topic": support.get("topic"), "title": "Repaso del material" if is_review else "Comprobación de refuerzo", "activity_kind": activity_kind, "created_at": response.get("created_at"), "correct": outcome, "used_help": used_help})
    topics = []
    labels = {"needs_support": "Conviene reforzar", "checking": "Por comprobar", "progressing": "Avance observado", "unscored": "Por contrastar"}
    for key, group in groups.items():
        events = sorted(group["events"], key=lambda e: (e["time"], e["kind"] == "reinforcement"))
        strategy, status, rationale, unresolved = _strategy(events, group["open_count"])
        practice_events = [e for e in events if e["kind"] == "practice"]
        topics.append({"id": hashlib.sha256("|".join(key).encode()).hexdigest()[:20], "unit_id": group["unit_id"], "topic": group["topic"], "objectives": group["objectives"], "attempt_count": len(group["attempt_ids"]), "correct_count": group["correct_count"], "incorrect_count": group["incorrect_count"], "graded_count": group["correct_count"] + group["incorrect_count"], "open_count": group["open_count"], "support_count": len(group["support_ids"]), "independently_correct_support_count": group["independently_correct_support_count"], "assisted_correct_support_count": group["assisted_correct_support_count"], "completed_source_review_count": group["completed_source_review_count"], "correct_source_review_count": group["correct_source_review_count"], "unresolved_error_count": unresolved, "status": status, "status_label": labels[status], "rationale": rationale, "last_activity_at": events[-1]["created_at"], "last_attempt_id": practice_events[-1]["attempt_id"], "recommended_strategy": strategy})
    priorities = {"needs_support": 0, "checking": 1, "unscored": 2, "progressing": 3}
    # A first error and a first correct response both need checking, but the
    # observed unresolved error takes priority over recency or alphabetical order.
    topics.sort(key=lambda t: (priorities[t["status"]], -t["unresolved_error_count"], -_time(t["last_activity_at"]), t["topic"]))
    timeline.sort(key=lambda e: (_time(e.get("created_at")), e["type"] == "reinforcement", e["id"]), reverse=True)
    objectives = {}
    for topic in topics:
        for objective in topic["objectives"]:
            entry = objectives.setdefault((topic["unit_id"], objective), {"objective": objective, "unit_id": topic["unit_id"], "topic_ids": []})
            entry["topic_ids"].append(topic["id"])
    summary = {"attempt_count": len(owned_attempts), "correct_count": sum(t["correct_count"] for t in topics), "graded_count": sum(t["graded_count"] for t in topics), "open_count": sum(t["open_count"] for t in topics), "support_count": len(supports), "completed_support_count": completed, "independently_correct_support_count": independent, "assisted_correct_support_count": assisted, "completed_source_review_count": completed_reviews, "correct_source_review_count": correct_reviews, "last_activity_at": timeline[0]["created_at"] if timeline else None}
    recommendation = None
    if topics:
        topic = topics[0]
        latest_attempt = attempts_by_id[topic["last_attempt_id"]]
        latest_support = max((r for r in supports if str(r["attempt_id"]) == topic["last_attempt_id"]), key=lambda r: (r.get("round_number", 1), _time(r.get("created_at"))), default=None)
        completed_check = bool(latest_support and latest_support.get("response"))
        recommendation = {"attempt_id": topic["last_attempt_id"], "reinforcement_id": latest_support["id"] if latest_support else None, "quiz_id": latest_attempt.get("quiz_id"), "action": "practice" if completed_check else "reinforcement", "topic": topic["topic"], "strategy": topic["recommended_strategy"], "label": "Hacer otra práctica" if completed_check else {"guided": "Repasar con pasos breves", "check": "Comprobar con otro caso", "transfer": "Aplicar en una nueva situación"}[topic["recommended_strategy"]], "rationale": topic["rationale"]}
    return {"summary": summary, "topics": topics, "timeline": timeline, "objectives": list(objectives.values()), "recommendation": recommendation}


def _case_text(value: str) -> str:
    return re.sub(r"^(?:Caso|Ejemplo) hipotético:\s*", "", str(value)).split("\n\n", 1)[0].strip()[:500]


def plan_reinforcement(profile: dict, attempt: dict, history: list[dict], reinforcements: list[dict], *, target_item_id: str | None = None) -> dict:
    """Freeze an explainable plan before generation, including the chosen source item."""
    if not profile.get("id") or attempt.get("student_id") != str(profile["id"]):
        raise ValueError("Este intento no pertenece al perfil seleccionado.")
    if not attempt.get("items"):
        raise ValueError("El intento no contiene respuestas para preparar un refuerzo.")
    unit_id = str(attempt.get("unit_id") or "")
    attempts, supports = _owned(profile, [*history, attempt], reinforcements)
    attempts = [a for a in attempts if str(a.get("unit_id") or "") == unit_id]
    # Freeze evidence available now, including later work if an old attempt is reopened.
    attempt_ids = {str(a["id"]) for a in attempts}
    supports = [r for r in supports if str(r["attempt_id"]) in attempt_ids]
    progress = summarize_progress(profile, attempts, supports)
    topic_lookup = {(t["unit_id"], _key(t["topic"])): t for t in progress["topics"]}
    if target_item_id is not None and not any(i.get("id") == target_item_id for i in attempt["items"]):
        raise ValueError("La pregunta seleccionada no pertenece a este intento.")
    candidates = [i for i in attempt["items"] if i.get("id") == target_item_id] if target_item_id is not None else [i for i in attempt["items"] if i.get("correct") is False]
    if not candidates:
        candidates = [i for i in attempt["items"] if i.get("correct") is None] or list(attempt["items"])
    order = {"guided": 0, "check": 1, "transfer": 2}
    target = min(candidates, key=lambda i: (order[topic_lookup[_topic_key(unit_id, i)]["recommended_strategy"]], -topic_lookup[_topic_key(unit_id, i)]["unresolved_error_count"], str(i.get("id") or "")))
    topic = topic_lookup[_topic_key(unit_id, target)]
    strategy = topic["recommended_strategy"]
    preferred = profile.get("presentation", "auto")
    if preferred not in {"auto", "visual", "steps"}:
        preferred = "auto"
    resolved = preferred if preferred != "auto" else "steps" if strategy == "guided" else "visual"
    reasoning = str(target.get("justification") or "").strip()
    steps = {
        "guided": ["Compara tu respuesta con la referencia revisada e identifica una diferencia.", "Revisa el ejemplo por partes y relaciona sus acciones con la idea del material.", "Resuelve un caso nuevo; si consultas ayuda, quedará registrado para el siguiente apoyo."],
        "check": ["Revisa la idea y un ejemplo breve del material.", "Prueba un caso nuevo para reunir más evidencia sobre esta idea."],
        "transfer": ["Aplica la misma idea en un contexto diferente.", "Intenta el caso nuevo antes de consultar el apoyo; después compara tu respuesta."],
    }[strategy]
    prior_cases = []
    for support in reversed(supports):
        if support.get("activity_kind") == "source_review":
            continue
        if _key(support.get("topic", "")) != _key(target.get("topic", "")):
            continue
        exercise = support.get("private_exercise") or support.get("exercise") or {}
        for text in (support.get("example", ""), exercise.get("question", "")):
            case = _case_text(text)
            if case and case not in prior_cases:
                prior_cases.append(case)
        if len(prior_cases) >= 6:
            break
    evidence_fields = ("attempt_count", "correct_count", "incorrect_count", "graded_count", "open_count", "support_count", "independently_correct_support_count", "assisted_correct_support_count", "completed_source_review_count", "correct_source_review_count", "unresolved_error_count")
    return {"policy_version": POLICY_VERSION, "student_id": str(profile["id"]), "target_item_id": target.get("id"), "unit_id": unit_id, "topic": target.get("topic") or "Contenido de la unidad", "strategy": strategy, "label": {"guided": "Apoyo guiado", "check": "Comprobación adicional", "transfer": "Aplicación en otro contexto"}[strategy], "rationale": topic["rationale"], "evidence": {field: topic[field] for field in evidence_fields}, "preferred_format": preferred, "resolved_format": resolved, "format_reason": "Tu preferencia guardada." if preferred != "auto" else "Pasos breves por errores repetidos en este tema." if strategy == "guided" else "Actividad visual cuando la fuente contiene relaciones verificables; en otro caso, apoyo por pasos.", "profile_snapshot": {field: profile.get(field) for field in ("id", "goals", "presentation", "updated_at")}, "reasoning_considered": False, "reasoning_note": "Tu razonamiento queda guardado para revisión; el apoyo se planifica con la respuesta y el historial disponible, sin calificarlo automáticamente." if reasoning else "No se registró una justificación. El apoyo se basa en la respuesta y en el historial disponible.", "scaffolding": steps, "previous_cases": prior_cases[:6]}
