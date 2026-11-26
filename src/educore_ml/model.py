"""Qwen formatting, supervision and shared inference."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import nullcontext
from pathlib import Path

import torch

BASE_MODEL = "Qwen/Qwen3-1.7B"
BASE_REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
MAX_NEW_TOKENS = 64
IGNORE_INDEX = -100

QUESTION_INSTRUCTIONS = (
    "Redacta exactamente una pregunta abierta en español usando el contexto. "
    "La pregunta debe poder responderse con la respuesta objetivo. "
    "Devuelve únicamente la pregunta, sin revelar la respuesta."
)
RESERVED_TEXT = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<think>", "</think>")


class ExampleTooLong(ValueError):
    """The evidence or target cannot fit without losing text."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_precision(precision: str) -> str:
    cuda = torch.cuda.is_available()
    if precision == "auto":
        if not cuda:
            return "fp32"
        return "bf16" if torch.cuda.is_bf16_supported(including_emulation=False) else "fp16"
    if precision == "bf16" and (not cuda or not torch.cuda.is_bf16_supported(including_emulation=False)):
        raise RuntimeError("BF16 requiere una GPU CUDA con soporte nativo.")
    if precision == "fp16" and not cuda:
        raise RuntimeError("FP16 requiere una GPU CUDA.")
    if precision not in {"bf16", "fp16", "fp32"}:
        raise ValueError(f"Precisión desconocida: {precision}")
    return precision


def load_tokenizer(adapter_dir: Path | None = None):
    from transformers import AutoTokenizer

    if adapter_dir is not None:
        return AutoTokenizer.from_pretrained(adapter_dir, local_files_only=True)
    return AutoTokenizer.from_pretrained(BASE_MODEL, revision=BASE_REVISION)


def load_base(precision: str):
    from transformers import AutoModelForCausalLM

    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]
    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    return AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        revision=BASE_REVISION,
        torch_dtype=dtype,
        device_map={"": device},
        attn_implementation="sdpa",
    )


def _reject_chat_markers(text: str) -> None:
    if any(marker in text for marker in RESERVED_TEXT):
        raise ValueError("La entrada contiene un marcador reservado de Qwen.")


def _chat_ids(tokenizer, instructions: str, content: str) -> list[int]:
    return tokenizer.apply_chat_template(
        [
            {"role": "system", "content": instructions},
            {"role": "user", "content": content},
        ],
        tokenize=True,
        add_generation_prompt=True,
        enable_thinking=False,
    )


def question_prompt_ids(
    tokenizer, context: str, answer: str, budget: int, answer_start: int | None = None
) -> list[int]:
    """Keep the exact answer and crop from both sides only if needed."""
    if not context.strip() or not answer.strip():
        raise ValueError("El contexto y la respuesta objetivo son obligatorios.")
    _reject_chat_markers(context)
    _reject_chat_markers(answer)
    start = context.find(answer) if answer_start is None else answer_start
    end = start + len(answer)
    if start < 0 or context[start:end] != answer:
        raise ValueError("La respuesta objetivo debe corresponder al fragmento del contexto.")
    left, right = 0, len(context)
    # Limit the first tokenization for very long pasted sources. It uses the same crop anchor.
    char_budget = max(budget * 12, 0)
    if right > len(answer) + 2 * char_budget:
        left, right = max(0, start - char_budget), min(right, end + char_budget)

    while True:
        excerpt = context[left:right]
        ids = _chat_ids(
            tokenizer, QUESTION_INSTRUCTIONS, f"Contexto:\n{excerpt}\n\nRespuesta objetivo:\n{answer}"
        )
        if len(ids) <= budget:
            return ids
        if left == start and right == end:
            raise ExampleTooLong("La respuesta y el formato de conversación exceden el presupuesto.")
        # Shorten the surrounding text geometrically while keeping the labeled span whole.
        left = start - (start - left) * 3 // 4
        right = end + (right - end) * 3 // 4


def encode_example(tokenizer, row: dict, max_length: int = 768) -> dict:
    question = row["question"].strip()
    if not question:
        raise ValueError("La pregunta de referencia está vacía.")
    _reject_chat_markers(question)
    target = tokenizer.encode(question, add_special_tokens=False)
    if not target:
        raise ValueError("La pregunta de referencia no tiene tokens.")
    # The output boundary is supervised along with the words of the question.
    target.append(tokenizer.eos_token_id)
    if len(target) > MAX_NEW_TOKENS:
        raise ExampleTooLong("La pregunta completa y su cierre exceden el presupuesto de salida.")
    prompt = question_prompt_ids(
        tokenizer, row["context"], row["answer"], max_length - MAX_NEW_TOKENS,
        row.get("answer_start"),
    )
    return {
        "input_ids": prompt + target,
        "attention_mask": [1] * (len(prompt) + len(target)),
        "labels": [IGNORE_INDEX] * len(prompt) + target,
    }


def collate_questions(tokenizer, rows: list[dict]) -> dict[str, torch.Tensor]:
    width = max(len(row["input_ids"]) for row in rows)
    padded = {"input_ids": [], "attention_mask": [], "labels": []}
    for row in rows:
        amount = width - len(row["input_ids"])
        padded["input_ids"].append(row["input_ids"] + [tokenizer.pad_token_id] * amount)
        padded["attention_mask"].append(row["attention_mask"] + [0] * amount)
        padded["labels"].append(row["labels"] + [IGNORE_INDEX] * amount)
    return {key: torch.tensor(value, dtype=torch.long) for key, value in padded.items()}


class QuestionEngine:
    """The CLI, notebook and API load the same model and exact adapter revision."""

    def __init__(
        self,
        adapter_dir: str | Path | None = None,
        *,
        precision: str = "auto",
        max_length: int | None = None,
    ):
        from peft import PeftModel, get_model_status

        metadata = {}
        directory = Path(adapter_dir) if adapter_dir is not None else None
        if directory is not None:
            metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            if metadata["base_model"] != BASE_MODEL or metadata["base_revision"] != BASE_REVISION:
                raise ValueError("El adaptador fue ajustado con una revisión base incompatible.")
            if not metadata["trainable_changed"] or metadata["global_step"] < 1:
                raise ValueError("El adaptador carece de evidencia de entrenamiento.")
            version = file_sha256(directory / "adapter_model.safetensors")
            if version != metadata["adapter_version"]:
                raise ValueError("El archivo del adaptador no coincide con su hash registrado.")

        self.precision = resolve_precision(precision)
        self.max_length = max_length if max_length is not None else metadata.get("max_length", 768)
        self.tokenizer = load_tokenizer(directory)
        self.model = load_base(self.precision)
        if directory is not None:
            self.model = PeftModel.from_pretrained(self.model, directory, is_trainable=False)
            self.model.set_adapter("default")
            self.model.requires_grad_(False)
            if get_model_status(self.model).enabled is not True:
                raise RuntimeError("El adaptador no quedó activo tras cargarlo.")
        self.model.eval()
        self.metadata = metadata
        self._lock = threading.Lock()

    def status(self) -> dict:
        return {
            "base_model": BASE_MODEL,
            "base_revision": BASE_REVISION,
            "adapter_active": bool(self.metadata),
            "adapter_version": self.metadata.get("adapter_version"),
            "profile": self.metadata.get("profile"),
            "precision": self.precision,
            "device": str(self.model.device),
            "max_length": self.max_length,
            "mode": "real",
        }

    def _generate(self, ids: list[int], max_new_tokens: int, use_adapter: bool) -> str:
        from transformers import GenerationConfig

        inputs = torch.tensor([ids], device=self.model.device)
        generation = GenerationConfig(
            max_new_tokens=max_new_tokens,
            do_sample=False,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.model.generation_config.eos_token_id,
        )
        with self._lock:
            adapter_scope = self.model.disable_adapter() if self.metadata and not use_adapter else nullcontext()
            with torch.inference_mode(), adapter_scope:
                output = self.model.generate(
                    input_ids=inputs,
                    attention_mask=torch.ones_like(inputs),
                    generation_config=generation,
                    use_model_defaults=False,
                )
        return self.tokenizer.decode(output[0, len(ids):], skip_special_tokens=True).strip()

    def generate(
        self,
        context: str,
        answer: str,
        *,
        max_new_tokens: int = MAX_NEW_TOKENS,
        use_adapter: bool = True,
        answer_start: int | None = None,
    ) -> str:
        if not 0 < max_new_tokens < self.max_length:
            raise ValueError("Presupuesto inválido para los tokens de la pregunta.")
        ids = question_prompt_ids(
            self.tokenizer, context, answer, self.max_length - max_new_tokens, answer_start
        )
        return self._generate(ids, max_new_tokens, use_adapter)

    def guidance(self, context: str, question: str, reference: str, attempt: str, *, practice=False) -> str:
        """Use the base with context for a distinct, untrained learning task."""
        instructions = (
            "Propón un ejercicio breve de refuerzo diferente que se pueda resolver con el contexto. "
            "No des la solución. No diagnostiques el dominio del estudiante."
            if practice else
            "Explica en español la respuesta de referencia en 2 o 3 frases usando el contexto. "
            "Invita a contrastar la idea central con el intento sin asignar una calificación."
        )
        content = (
            f"Contexto:\n{context}\n\nPregunta:\n{question}\n\n"
            f"Referencia:\n{reference}\n\nIntento del estudiante:\n{attempt}"
        )
        _reject_chat_markers(content)
        ids = _chat_ids(self.tokenizer, instructions, content)
        max_new_tokens = 128
        if len(ids) + max_new_tokens > self.model.config.max_position_embeddings:
            raise ExampleTooLong("La fuente y el intento exceden el contexto disponible para esta actividad.")
        return self._generate(ids, max_new_tokens, use_adapter=False)
