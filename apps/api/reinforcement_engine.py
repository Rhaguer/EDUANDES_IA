"""Local inference for new reinforcement cases, separate from the trained adapter."""
from __future__ import annotations

from pathlib import Path
from threading import Lock

from .text_safety import ExampleTooLong, reject_chat_markers as _reject_chat_markers


BASE_MODEL = "Qwen/Qwen3-4B-Instruct-2507"
BASE_REVISION = "cdbee75f17c01a7cc42f958dc650907174af0554"


class LocalReinforcementEngine:
    """Load the pinned, cached instruction model only when explicitly constructed."""

    def __init__(self, model_path: str | Path | None = None, *, cache_dir: str | Path | None = None):
        import torch

        if not torch.cuda.is_available():
            raise RuntimeError("El motor local de refuerzos requiere una GPU CUDA disponible.")
        if not torch.cuda.is_bf16_supported(including_emulation=False):
            raise RuntimeError("El motor local de refuerzos requiere una GPU CUDA con soporte BF16 nativo.")

        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

        source = str(model_path) if model_path is not None else BASE_MODEL
        loading = {"revision": BASE_REVISION, "local_files_only": True}
        if cache_dir is not None:
            loading["cache_dir"] = str(cache_dir)
        self.tokenizer = AutoTokenizer.from_pretrained(source, **loading)
        quantization = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
        self.model = AutoModelForCausalLM.from_pretrained(
            source, **loading, quantization_config=quantization,
            torch_dtype=torch.bfloat16, device_map={"": "cuda:0"}, attn_implementation="sdpa",
        )
        self.model.eval()
        self._lock = Lock()

    def status(self) -> dict:
        return {
            "base_model": BASE_MODEL, "base_revision": BASE_REVISION,
            "adapter_active": False, "adapter_version": None, "profile": None,
            "precision": "nf4", "compute_precision": "bf16", "device": str(self.model.device),
            "thinking": False, "mode": "real", "reinforcement_choices": "generated",
        }

    def generate_course_reinforcement(self, attempt: dict, source_page: dict) -> dict:
        from .reinforcement_generation import generate_reinforcement
        return generate_reinforcement(self, attempt, source_page)

    def course_completion(self, instructions: str, content: str, *, creative: bool, seed: int, tokens: int) -> str:
        import torch
        from transformers import GenerationConfig

        _reject_chat_markers(instructions)
        _reject_chat_markers(content)
        if type(tokens) is not int or tokens <= 0:
            raise ValueError("El presupuesto de generación debe ser un entero positivo.")
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": content}]
        ids = self.tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=True, enable_thinking=False,
        )
        if len(ids) + tokens > self.model.config.max_position_embeddings:
            raise ExampleTooLong("La fuente excede el contexto disponible para este refuerzo.")
        inputs = torch.tensor([ids], device=self.model.device)
        settings = {"max_new_tokens": tokens, "do_sample": creative,
                    "pad_token_id": self.tokenizer.pad_token_id,
                    "eos_token_id": self.model.generation_config.eos_token_id}
        if creative:
            settings.update(temperature=0.7, top_p=0.8, top_k=20)
        devices = [self.model.device.index or 0] if self.model.device.type == "cuda" else []
        with self._lock, torch.random.fork_rng(devices=devices):
            torch.manual_seed(seed)
            with torch.inference_mode():
                output = self.model.generate(
                    input_ids=inputs, attention_mask=torch.ones_like(inputs),
                    generation_config=GenerationConfig(**settings), use_model_defaults=False,
                )
        # This model produces the final answer directly; no thinking-block parser.
        return self.tokenizer.decode(output[0, len(ids):], skip_special_tokens=True).strip()
