"""Run reproducible answer-aware question generation fine-tuning."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import subprocess
import time
from collections import Counter
from functools import partial
from pathlib import Path

import torch
import yaml

from .data import read_rows
from .model import (
    BASE_MODEL,
    BASE_REVISION,
    IGNORE_INDEX,
    MAX_NEW_TOKENS,
    ExampleTooLong,
    QuestionEngine,
    collate_questions,
    encode_example,
    file_sha256,
    load_tokenizer,
    resolve_precision,
)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _digest_parameters(model) -> str:
    digest = hashlib.sha256()
    for name, parameter in model.named_parameters():
        if parameter.requires_grad:
            digest.update(name.encode("utf-8"))
            digest.update(parameter.detach().to(device="cpu", dtype=torch.float32).numpy().tobytes())
    return digest.hexdigest()


def _tokenize_rows(rows, tokenizer, max_length):
    encoded, accepted, discarded = [], [], []
    for row in rows:
        try:
            encoded.append(encode_example(tokenizer, row, max_length))
            accepted.append(row)
        except ExampleTooLong as error:
            discarded.append({"id": row["id"], "reason": str(error)})
    if not encoded:
        raise ValueError("Ningún ejemplo cabe en el presupuesto configurado.")
    return encoded, accepted, discarded


def inspect_mask(config: Path, data_dir: Path) -> dict:
    settings = yaml.safe_load(Path(config).read_text(encoding="utf-8-sig"))
    tokenizer = load_tokenizer()
    for row in read_rows(Path(data_dir) / "train.jsonl"):
        try:
            tokens = encode_example(tokenizer, row, settings["max_length"])
        except ExampleTooLong:
            continue
        supervised = [label for label in tokens["labels"] if label != IGNORE_INDEX]
        prefix_count = len(tokens["labels"]) - len(supervised)
        padded = collate_questions(tokenizer, [tokens, {key: value[:-1] for key, value in tokens.items()}])
        decoded = tokenizer.decode(supervised, skip_special_tokens=True)
        return {
            "id": row["id"],
            "expected_question": row["question"].strip(),
            "supervised_text": decoded,
            "supervised_matches_reference": decoded == row["question"].strip(),
            "supervised_token_ids": supervised,
            "supervised_end_token": tokenizer.convert_ids_to_tokens(supervised[-1]),
            "masked_prompt_tokens": prefix_count,
            "prompt_all_masked": all(x == IGNORE_INDEX for x in tokens["labels"][:prefix_count]),
            "padding_all_masked": bool((padded["labels"][padded["attention_mask"] == 0] == IGNORE_INDEX).all()),
        }
    raise ValueError("No hay una fila representativa para inspeccionar.")


def _runtime() -> dict:
    versions = {
        name: importlib.metadata.version(name)
        for name in ("torch", "transformers", "peft", "accelerate", "huggingface-hub", "datasets")
    }
    gpu = torch.cuda.get_device_properties(0)
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": versions,
        "cuda": torch.version.cuda,
        "gpu": gpu.name,
        "gpu_total_bytes": gpu.total_memory,
        "gpu_free_bytes_at_start": torch.cuda.mem_get_info(0)[0],
    }


def _code_version() -> dict:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False,
            cwd=Path(__file__).resolve().parents[2],
        )
        revision = result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        revision = None
    directory = Path(__file__).parent
    return {
        "git_revision": revision,
        "source_sha256": {name: file_sha256(directory / name) for name in ("model.py", "train.py")},
    }


def train(config: Path, data_dir: Path, run_dir: Path, resume: Path | None = None) -> dict:
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import Trainer, TrainerCallback, TrainingArguments, set_seed

    if not torch.cuda.is_available():
        raise RuntimeError("Se requiere una GPU CUDA disponible para este entrenamiento LoRA.")
    config = Path(config)
    data_dir = Path(data_dir)
    run_dir = Path(run_dir).resolve()
    settings = yaml.safe_load(config.read_text(encoding="utf-8-sig"))
    train_path, validation_path = data_dir / "train.jsonl", data_dir / "validation.jsonl"
    data_hashes = {path.name: file_sha256(path) for path in (train_path, validation_path)}
    set_seed(settings["seed"])

    manifest_path = run_dir / "run.json"
    if resume is not None:
        resume = Path(resume).resolve()
        checkpoint_state = json.loads((resume / "trainer_state.json").read_text(encoding="utf-8"))
        if resume.parent != run_dir / "checkpoints" or checkpoint_state["global_step"] <= 0:
            raise ValueError("El checkpoint no pertenece a esta ejecución.")
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if recorded["config"] != settings or recorded["data_sha256"] != data_hashes:
            raise ValueError("La configuración o los datos difieren de la ejecución guardada.")
    elif run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError("Usa un directorio de ejecución nuevo o indica un checkpoint con --resume.")
    run_dir.mkdir(parents=True, exist_ok=True)

    runtime = _runtime()
    precision = resolve_precision(settings["precision"])
    if resume is not None and recorded["precision"] != precision:
        raise ValueError("La precisión actual difiere de la ejecución guardada.")
    manifest = {
        "config": settings,
        "data_sha256": data_hashes,
        "base_model": BASE_MODEL,
        "base_revision": BASE_REVISION,
        "precision": precision,
        "runtime": runtime,
        "code": _code_version(),
    }
    if resume is None:
        _write_json(manifest_path, manifest)
    max_length = settings["max_length"]
    engine = QuestionEngine(precision=precision, max_length=max_length)
    tokenizer = engine.tokenizer
    train_rows = read_rows(train_path)
    val_rows = read_rows(validation_path)
    train_tokens, _, discarded_train = _tokenize_rows(train_rows, tokenizer, max_length)
    val_tokens, val_kept, discarded_val = _tokenize_rows(val_rows, tokenizer, max_length)
    _write_json(run_dir / "tokenization.json", {
        "train": {"before": len(train_rows), "after": len(train_tokens), "discarded": discarded_train},
        "validation": {"before": len(val_rows), "after": len(val_tokens), "discarded": discarded_val},
    })

    baseline_path = run_dir / "baseline_validation.jsonl"
    if not baseline_path.exists():
        baseline = []
        for row in val_kept[:settings["baseline_examples"]]:
            print(f"Baseline de validación {len(baseline) + 1}: {row['id']}", flush=True)
            start = time.perf_counter()
            generated = engine.generate(row["context"], row["answer"], answer_start=row["answer_start"])
            baseline.append({
                **row, "base_question": generated, "latency_seconds": time.perf_counter() - start,
                "base_revision": BASE_REVISION, "precision": precision,
                "generation": {"max_new_tokens": MAX_NEW_TOKENS, "do_sample": False},
            })
        baseline_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in baseline), encoding="utf-8"
        )

    targets = ("q_proj", "v_proj")
    module_counts = Counter(
        name.split(".")[-1]
        for name, layer in engine.model.named_modules()
        if isinstance(layer, torch.nn.Linear) and name.split(".")[-1] in targets
    )
    if set(module_counts) != set(targets):
        raise RuntimeError(f"Módulos LoRA incompatibles con la arquitectura: {dict(module_counts)}")
    set_seed(settings["seed"])
    model = get_peft_model(engine.model, LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        target_modules=list(targets),
        r=settings["rank"],
        lora_alpha=settings["alpha"],
        lora_dropout=settings["dropout"],
        bias="none",
        revision=BASE_REVISION,
    ))
    del engine
    model.config.use_cache = False
    trainable, total = model.get_nb_trainable_parameters()
    initial_hash = _digest_parameters(model)

    class SaveFinalStep(TrainerCallback):
        def on_step_end(self, args, state, control, **kwargs):
            if state.global_step == state.max_steps:
                control.should_evaluate = True
                control.should_save = True

    args = TrainingArguments(
        output_dir=str(run_dir / "checkpoints"),
        learning_rate=settings["learning_rate"],
        per_device_train_batch_size=settings["batch_size"],
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=settings["gradient_accumulation"],
        num_train_epochs=settings["epochs"],
        max_steps=settings["max_steps"],
        warmup_ratio=0.05,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        bf16=precision == "bf16",
        fp16=precision == "fp16",
        optim="adamw_torch",
        logging_strategy="steps",
        logging_steps=1 if settings["profile"] == "smoke" else 10,
        eval_strategy="steps",
        eval_steps=settings["eval_steps"],
        save_strategy="steps",
        save_steps=settings["eval_steps"],
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        prediction_loss_only=True,
        remove_unused_columns=False,
        label_names=["labels"],
        dataloader_num_workers=0,
        report_to=[],
        seed=settings["seed"],
        data_seed=settings["seed"],
        disable_tqdm=True,
    )
    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_tokens,
        eval_dataset=val_tokens,
        processing_class=tokenizer,
        data_collator=partial(collate_questions, tokenizer),
        callbacks=[SaveFinalStep()],
    )
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats(0)
    started = time.perf_counter()
    trained = trainer.train(resume_from_checkpoint=str(resume) if resume else None)
    elapsed = time.perf_counter() - started
    final_hash = _digest_parameters(model)
    if trainer.state.global_step <= 0 or final_hash == initial_hash:
        raise RuntimeError("El entrenamiento no produjo una actualización verificable del adaptador.")
    if not trainer.state.best_model_checkpoint or not math.isfinite(trainer.state.best_metric):
        raise RuntimeError("El entrenamiento no produjo un checkpoint seleccionado por validación.")

    export = run_dir / "adapter"
    model.save_pretrained(export, safe_serialization=True)
    tokenizer.save_pretrained(export)
    metadata = {
        "base_model": BASE_MODEL,
        "base_revision": BASE_REVISION,
        "adapter_version": file_sha256(export / "adapter_model.safetensors"),
        "precision": precision,
        "max_length": max_length,
        "profile": settings["profile"],
        "global_step": trainer.state.global_step,
        "adapter_initial_sha256": initial_hash,
        "adapter_final_sha256": final_hash,
        "trainable_changed": True,
        "selected_step": int(Path(trainer.state.best_model_checkpoint).name.rsplit("-", 1)[1]),
    }
    _write_json(export / "metadata.json", metadata)
    metrics = {
        **metadata,
        "trainable_parameters": trainable,
        "total_parameters": total,
        "target_module_counts": dict(module_counts),
        "best_validation_loss": trainer.state.best_metric,
        "best_checkpoint": Path(trainer.state.best_model_checkpoint).name,
        "train_rows_available": len(train_tokens),
        "validation_rows": len(val_tokens),
        "effective_epochs": trainer.state.epoch,
        "examples_consumed_approx": round(trainer.state.epoch * len(train_tokens)),
        "duration_seconds": elapsed,
        "cuda_peak_allocated_bytes": torch.cuda.max_memory_allocated(0),
        "cuda_peak_reserved_bytes": torch.cuda.max_memory_reserved(0),
        "train_metrics": trained.metrics,
        "history": trainer.state.log_history,
    }
    _write_json(run_dir / "metrics.json", metrics)
    return metrics
