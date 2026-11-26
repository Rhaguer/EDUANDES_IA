"""Descarga y preparación reproducible de los JSON oficiales de SQAC."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import random
from statistics import median
from tempfile import NamedTemporaryFile
import unicodedata


DATASET = "PlanTL-GOB-ES/SQAC"
REVISION = "f9928e8819596a601b8887cc5f8598b15d589a82"
LICENSE = "CC-BY-SA-4.0"
FILES = {
    "train": ("train.json", "1d5c76176646e2ae7bdcd8b5ec6f18349102a9363aa25ad7d0e48262d7480d43"),
    "validation": ("dev.json", "ec748f222626e5081a34193ca469bcf8112092837678a6948a2d6ae7d6629d1a"),
    "test": ("test.json", "af84d20d4afeba9a6936c3c7d8b5f577a15af5461c8f0e29f94286bb2205b18d"),
}


def raw_manifest(raw_dir: Path) -> dict:
    files = {}
    for split, (name, expected) in FILES.items():
        path = raw_dir / name
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"SHA-256 inesperado en {path}. Descarga la revisión {REVISION}.")
        files[split] = {"name": name, "sha256": actual, "bytes": path.stat().st_size}
    return {"dataset": DATASET, "revision": REVISION, "license": LICENSE, "files": files}


def download(raw_dir: Path) -> dict:
    from huggingface_hub import hf_hub_download

    for name, _ in FILES.values():
        hf_hub_download(
            DATASET, name, repo_type="dataset", revision=REVISION,
            local_dir=raw_dir, token=False,
        )
    manifest = raw_manifest(raw_dir)
    write_json(raw_dir / "manifest.json", manifest)
    return manifest


def normalize_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def write_text(path: Path, content: str) -> None:
    temporary = NamedTemporaryFile(dir=path.parent, delete=False)
    staging_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(content.encode("utf-8"))
        staging_path.replace(path)
    finally:
        staging_path.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def read_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def span_error(context: str, answer: str, start: int) -> str | None:
    if not nonempty(answer):
        return "empty_answer"
    if type(start) is not int or not 0 <= start <= len(context) - len(answer):
        return "invalid_offset"
    if context[start:start + len(answer)] == answer:
        return None
    return "offset_mismatch" if answer in context else "answer_not_in_context"


@dataclass
class Split:
    rows: list[dict]
    documents: dict[str, set[str]]
    question_ids: set[str]
    stats: dict


def read_split(path: Path) -> Split:
    data = json.loads(path.read_bytes())
    articles = data["data"]
    rows = []
    documents = defaultdict(set)
    ids = set()
    kept_ids = set()
    pairs = set()
    rejected = Counter()
    sources = Counter()
    paragraphs = questions = 0
    for article in articles:
        source, title = article.get("source"), article.get("title")
        document_id = None
        if nonempty(source) and nonempty(title):
            document_id = text_hash(json.dumps([normalize_key(source), normalize_key(title)], ensure_ascii=False))
            sources[source] += 1
            documents.setdefault(document_id, set())
        for paragraph in article["paragraphs"]:
            paragraphs += 1
            context = paragraph.get("context")
            context_key = text_hash(normalize_key(context)) if nonempty(context) else None
            if document_id and context_key:
                documents[document_id].add(context_key)
            for qa in paragraph["qas"]:
                questions += 1
                id_, question = qa.get("id"), qa.get("question")
                if nonempty(id_):
                    ids.add(id_)
                if not (document_id and context_key and nonempty(id_) and nonempty(question)):
                    rejected["empty_required_field"] += 1
                    continue
                # Los archivos de esta revisión contienen una respuesta por pregunta.
                answers = qa.get("answers")
                if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
                    rejected["unexpected_answer_count_or_shape"] += 1
                    continue
                answer, start = answers[0].get("text"), answers[0].get("answer_start")
                reason = span_error(context, answer, start)
                if reason:
                    rejected[reason] += 1
                    continue
                pair = (context_key, normalize_key(question), normalize_key(answer))
                if id_ in kept_ids or pair in pairs:
                    rejected["duplicate_id" if id_ in kept_ids else "duplicate_example"] += 1
                    continue
                kept_ids.add(id_)
                pairs.add(pair)
                rows.append({
                    "id": id_, "document_id": document_id, "source": source, "title": title,
                    "context": context, "context_sha256": context_key,
                    "answer": answer, "answer_start": start, "question": question,
                })
    before_duplicates = len(rows) + rejected["duplicate_example"] + rejected["duplicate_id"]
    stats = {
        "documents_read": len(articles), "paragraphs_read": paragraphs,
        "questions_read": questions,
        "questions_after_nonempty_fields": questions - rejected["empty_required_field"]
        - rejected["unexpected_answer_count_or_shape"] - rejected["empty_answer"],
        "questions_after_span_validation": before_duplicates,
        "questions_after_deduplication": len(rows),
        "rejected": dict(rejected), "documents_by_source": dict(sources),
        "lengths_clean": lengths(rows),
    }
    return Split(rows, dict(documents), ids, stats)


def lengths(rows: list[dict]) -> dict:
    if not rows:
        return {}
    summary = {}
    for field in ("context", "question", "answer"):
        for unit, count in (("chars", len), ("words", lambda text: len(text.split()))):
            values = sorted(count(row[field]) for row in rows)
            summary[f"{field}_{unit}"] = {
                "min": values[0], "median": median(values),
                "p95": values[(95 * len(values) - 1) // 100], "max": values[-1],
            }
    return summary


def keys(split: Split) -> dict[str, set[str]]:
    return {
        "documents": set(split.documents),
        "contexts": set().union(*split.documents.values()),
        "question_ids": split.question_ids,
    }


def overlaps(partitions: dict[str, dict[str, set[str]]]) -> dict:
    return {
        f"{left}:{right}": {key: len(partitions[left][key] & partitions[right][key]) for key in partitions[left]}
        for left, right in combinations(partitions, 2)
    }


def row_keys(rows: list[dict]) -> dict[str, set[str]]:
    return {
        "documents": {row["document_id"] for row in rows},
        "contexts": {row["context_sha256"] for row in rows},
        "question_ids": {row["id"] for row in rows},
    }


def isolate(splits: dict[str, Split]) -> tuple[dict[str, list[dict]], dict]:
    raw = {name: keys(split) for name, split in splits.items()}
    reserve = keys(splits["test"])
    retained = {"test": splits["test"].rows}
    dropped = {}
    for name in ("validation", "train"):
        split = splits[name]
        blocked = set(split.documents) & reserve["documents"]
        blocked.update(doc for doc, contexts in split.documents.items() if contexts & reserve["contexts"])
        blocked.update(row["document_id"] for row in split.rows if row["id"] in reserve["question_ids"])
        retained[name] = [row for row in split.rows if row["document_id"] not in blocked]
        dropped[name] = {"document_ids": sorted(blocked), "questions": len(split.rows) - len(retained[name])}
        if name == "validation":
            reserve = {key: reserve[key] | values for key, values in row_keys(retained[name]).items()}
    after = overlaps({name: row_keys(rows) for name, rows in retained.items()})
    if any(value for counts in after.values() for value in counts.values()):
        raise ValueError("Persisten solapamientos entre particiones tras el aislamiento.")
    return retained, {"official_overlap": overlaps(raw), "retained_overlap": after, "dropped": dropped}


def sample_documents(rows: list[dict], limit: int, seed: int) -> list[dict]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["document_id"]].append(row)
    ids = sorted(grouped)
    random.Random(seed).shuffle(ids)
    sample = []
    for id_ in ids:
        group = grouped[id_]
        if len(sample) + len(group) <= limit:
            sample.extend(group)
        if len(sample) == limit:
            break
    return sample


def save_sample(directory: Path, rows: list[dict], name: str, limit: int, seed: int) -> dict:
    sample = sample_documents(rows, limit, seed)
    if not sample:
        raise ValueError(f"Sin documentos completos para {name} con un límite de {limit} ejemplos.")
    path = directory / f"{name}.jsonl"
    write_text(path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in sample))
    return {
        "limit": limit, "questions": len(sample), "name": path.name,
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "question_ids": [row["id"] for row in sample],
        "document_ids": sorted({row["document_id"] for row in sample}),
        "context_sha256": sorted({row["context_sha256"] for row in sample}),
        "lengths": lengths(sample),
    }


def prepare(
    raw_dir: Path, output_dir: Path, *, seed: int = 42, smoke_train: int = 64,
    smoke_validation: int = 16, train_limit: int = 2000,
    validation_limit: int = 200, test_limit: int = 200,
) -> dict:
    provenance = raw_manifest(raw_dir)
    splits = {name: read_split(raw_dir / filename) for name, (filename, _) in FILES.items()}
    rows, audit = isolate(splits)
    manifest = {
        **provenance, "seed": seed, "audit": audit,
        "splits": {
            name: {**split.stats, "questions_after_isolation": len(rows[name])}
            for name, split in splits.items()
        },
        "samples": {},
    }
    configs = {
        "smoke": {"train": smoke_train, "validation": smoke_validation},
        "train": {"train": train_limit, "validation": validation_limit, "test": test_limit},
    }
    for mode, limits in configs.items():
        directory = output_dir / mode
        directory.mkdir(parents=True, exist_ok=True)
        samples = {name: save_sample(directory, rows[name], name, limit, seed) for name, limit in limits.items()}
        write_json(directory / "manifest.json", {**provenance, "seed": seed, "mode": mode, "samples": samples})
        manifest["samples"][mode] = samples
    write_json(raw_dir / "manifest.json", provenance)
    write_json(output_dir / "manifest.json", manifest)
    return manifest
