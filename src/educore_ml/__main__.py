"""Comandos de preparación, ajuste, evaluación y recarga."""

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys


def doctor():
    import psutil
    import torch

    packages = {}
    for name in ("torch", "transformers", "peft", "accelerate", "datasets", "huggingface-hub"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    report = {
        "python": platform.python_version(),
        "system": platform.platform(),
        "ram_gib": round(psutil.virtual_memory().total / 2**30, 2),
        "packages": packages,
        "cuda_available": torch.cuda.is_available(),
        "cuda_runtime": torch.version.cuda,
    }
    if report["cuda_available"]:
        free, total = torch.cuda.mem_get_info()
        report.update(
            gpu=torch.cuda.get_device_name(),
            vram_free_gib=round(free / 2**30, 2),
            vram_total_gib=round(total / 2**30, 2),
            bf16=torch.cuda.is_bf16_supported(including_emulation=False),
        )
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    os.environ.setdefault("HF_HOME", str(Path(".cache/huggingface").resolve()))
    parser = argparse.ArgumentParser(prog="educore_ml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Diagnosticar Python, CUDA y dependencias")
    commands.add_parser("fetch-model", help="Descargar la revisión fijada de Qwen3")
    prepare = commands.add_parser("prepare", help="Descargar y preparar SQAC")
    prepare.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    prepare.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--train-limit", type=int, default=2000)
    prepare.add_argument("--validation-limit", type=int, default=200)
    prepare.add_argument("--test-limit", type=int, default=200)

    for command in ("train", "inspect"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--config", type=Path, required=True)
        subparser.add_argument("--data-dir", type=Path)
        if command == "train":
            subparser.add_argument("--run-dir", type=Path, required=True)
            subparser.add_argument("--resume", type=Path)

    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--run-dir", type=Path, required=True)
    evaluation.add_argument("--split", choices=("validation", "test"), required=True)
    evaluation.add_argument("--data-dir", type=Path)
    evaluation.add_argument("--output-dir", type=Path, required=True)
    evaluation.add_argument("--academic", type=Path)
    evaluation.add_argument("--limit", type=int, help="Evaluar una muestra reproducible de documentos")
    evaluation.add_argument(
        "--precision", choices=("auto", "bf16", "fp16", "fp32"), default="auto",
        help="auto usa BF16 con soporte nativo y FP16 en GPU sin BF16 (p. ej., T4 de Colab)",
    )

    infer = commands.add_parser("infer", help="Recargar un adaptador y generar en un proceso nuevo")
    infer.add_argument("--run-dir", type=Path, required=True)
    infer.add_argument("--context", required=True)
    infer.add_argument("--answer", required=True)
    infer.add_argument("--max-new-tokens", type=int, default=64)
    args = parser.parse_args()

    try:
        if args.command == "doctor":
            result = doctor()
        elif args.command == "fetch-model":
            from huggingface_hub import snapshot_download

            from .model import BASE_MODEL, BASE_REVISION

            snapshot = snapshot_download(
                BASE_MODEL, revision=BASE_REVISION, max_workers=1,
                allow_patterns=["*.json", "*.safetensors", "merges.txt", "LICENSE"],
            )
            result = {"base_model": BASE_MODEL, "revision": BASE_REVISION, "snapshot": snapshot}
        elif args.command == "prepare":
            from .data import download, prepare

            download(args.raw_dir)
            result = prepare(
                args.raw_dir, args.output_dir, seed=args.seed, train_limit=args.train_limit,
                validation_limit=args.validation_limit, test_limit=args.test_limit,
            )
        elif args.command in ("train", "inspect"):
            from .train import inspect_mask, train

            data_dir = args.data_dir or Path("data/processed") / args.config.stem
            if args.command == "train":
                result = train(args.config, data_dir, args.run_dir, resume=args.resume)
            else:
                result = inspect_mask(args.config, data_dir)
        elif args.command == "evaluate":
            from .evaluate import evaluate

            metadata = json.loads((args.run_dir / "adapter" / "metadata.json").read_text("utf-8"))
            data_dir = args.data_dir or Path("data/processed") / metadata["profile"]
            result = evaluate(
                args.run_dir, data_dir / f"{args.split}.jsonl", args.output_dir,
                split=args.split, academic_path=args.academic, limit=args.limit,
                precision=args.precision,
            )
        else:
            from .model import QuestionEngine

            engine = QuestionEngine(args.run_dir / "adapter")
            question = engine.generate(args.context, args.answer, max_new_tokens=args.max_new_tokens)
            if not question.strip():
                raise RuntimeError("El modelo devolvió una pregunta vacía.")
            result = {"question": question, "engine": engine.status()}
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"EduCore: {error}\n")

    if isinstance(result, dict) and args.command == "prepare":
        result = {"manifest": str(args.output_dir / "manifest.json")}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
