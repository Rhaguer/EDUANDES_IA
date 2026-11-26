"""Versioned learning prompts for the app, independent of training artifacts."""

import re
import unicodedata
from contextlib import nullcontext

from educore_ml.model import QuestionEngine, _chat_ids
from .text_safety import ExampleTooLong, reject_chat_markers as _reject_chat_markers


REINFORCEMENT_PROMPT_VERSION = "open-application-v1"
REINFORCEMENT_TOKENS = 160
REINFORCEMENT_INSTRUCTIONS = (
    "Escribe en español exactamente un ejercicio breve de aplicación, con una situación "
    "concreta nueva y una sola pregunta abierta que se pueda resolver con el material. "
    "El estudiante ya respondió la pregunta original: NO la repitas ni la reformules. "
    "Hazle aplicar la idea a otra situación, predecir una consecuencia o justificar qué "
    "ocurriría al cambiar una condición descrita en el material. "
    "Usa únicamente los hechos del material; no añadas conocimientos necesarios que no aparezcan allí. "
    "Devuelve solo el enunciado del nuevo ejercicio. Sin opciones, listas, respuesta, solución, "
    "explicación ni calificación. No diagnostiques el dominio del estudiante."
)
# Qwen3 thinking-mode sampling, shared by proposals (creative=True) and checks
# (creative=False). Greedy decoding is not used with thinking enabled.
THINKING_SAMPLING = {"do_sample": True, "temperature": 0.6, "top_p": 0.95, "top_k": 20}


def _normalized(text: str) -> str:
    without_accents = "".join(
        character for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join("".join(character if character.isalnum() else " " for character in without_accents).split())


def _rejection_reason(text: str, question: str) -> str | None:
    normalized_question = _normalized(question)
    if not text.strip():
        return "El ejercicio quedó vacío."
    if normalized_question and normalized_question in _normalized(text):
        return "El ejercicio repite la pregunta original, que ya fue respondida."
    if re.search(r"(?:^|\s)[a-dA-D][).]\s", text):
        return "El ejercicio incluye opciones; debe ser una pregunta abierta."
    if re.search(r"\b(?:respuesta|soluci[oó]n)\s*:", text, flags=re.IGNORECASE):
        return "El ejercicio incluye una respuesta o solución."
    return None


class LearningEngine(QuestionEngine):
    """Keep trained question generation unchanged and improve app-only reinforcement."""

    reinforcement_prompt_version = REINFORCEMENT_PROMPT_VERSION

    def status(self) -> dict:
        return {**super().status(), "thinking": False}

    def generate_course_item(self, page: dict, objective: str, item_type: str, previous_questions: list[str], *, previous_answers: list[str] | None = None, style_index: int = 0) -> dict:
        from .course_generation import generate_item
        return generate_item(self, page, objective, item_type, previous_questions, previous_answers=previous_answers, style_index=style_index)

    def complete_structured(self, instructions: str, content: str, *, creative: bool = False, seed: int = 0, tokens: int = 750) -> str:
        import torch
        from transformers import GenerationConfig

        _reject_chat_markers(content)
        ids = _chat_ids(self.tokenizer, instructions, content)
        if len(ids) + tokens > self.model.config.max_position_embeddings:
            raise ValueError("El fragmento excede la capacidad del modelo. Selecciona un material más breve.")
        if not creative:
            return self._generate(ids, tokens, use_adapter=False)
        inputs = torch.tensor([ids], device=self.model.device)
        generation = GenerationConfig(
            max_new_tokens=tokens,
            do_sample=True,
            temperature=0.65,
            top_p=0.9,
            top_k=30,
            pad_token_id=self.tokenizer.pad_token_id,
            eos_token_id=self.model.generation_config.eos_token_id,
        )
        devices = [self.model.device.index or 0] if self.model.device.type == "cuda" else []
        with self._lock, torch.random.fork_rng(devices=devices):
            torch.manual_seed(seed or 42)
            adapter_scope = self.model.disable_adapter() if self.metadata else nullcontext()
            with torch.inference_mode(), adapter_scope:
                output = self.model.generate(
                    input_ids=inputs,
                    attention_mask=torch.ones_like(inputs),
                    generation_config=generation,
                    use_model_defaults=False,
                )
        return self.tokenizer.decode(output[0, len(ids):], skip_special_tokens=True).strip()

    def generate_course_reinforcement(self, attempt: dict, source_page: dict) -> dict:
        from .reinforcement_generation import generate_reinforcement
        return generate_reinforcement(self, attempt, source_page)

    def course_completion(self, instructions: str, content: str, *, creative: bool, seed: int, tokens: int) -> str:
        """Fast direct app-only decoding without slow hidden thinking tokens."""
        import torch
        from transformers import GenerationConfig

        if type(creative) is not bool:
            raise TypeError("creative debe ser True (propuesta) o False (verificación).")
        _reject_chat_markers(content)
        messages = [{"role": "system", "content": instructions}, {"role": "user", "content": content}]
        ids = self.tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True, enable_thinking=False)
        if len(ids) + tokens > self.model.config.max_position_embeddings:
            raise ExampleTooLong("La fuente excede el contexto disponible para este refuerzo.")
        inputs = torch.tensor([ids], device=self.model.device)
        settings = {"max_new_tokens": tokens, "do_sample": creative,
                    "pad_token_id": self.tokenizer.pad_token_id,
                    "eos_token_id": self.model.generation_config.eos_token_id}
        if creative:
            settings.update(temperature=0.65, top_p=0.9, top_k=20)
        devices = [self.model.device.index or 0] if self.model.device.type == "cuda" else []
        with self._lock, torch.random.fork_rng(devices=devices):
            torch.manual_seed(seed)
            adapter_scope = self.model.disable_adapter() if self.metadata else nullcontext()
            with torch.inference_mode(), adapter_scope:
                output = self.model.generate(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                    generation_config=GenerationConfig(**settings), use_model_defaults=False)
        return self.tokenizer.decode(output[0, len(ids):], skip_special_tokens=True).strip()

    def guidance(self, context: str, question: str, reference: str, attempt: str, *, practice=False) -> str:
        if not practice:
            return super().guidance(context, question, reference, attempt, practice=False)

        content = (
            f"Material de estudio:\n{context}\n\n"
            f"Pregunta original YA RESPONDIDA (prohibido repetirla):\n{question}\n\n"
            f"Referencia de esa pregunta, no es la solución del nuevo ejercicio:\n{reference}\n\n"
            f"Intento previo del estudiante:\n{attempt}\n\n"
            "Propón ahora una situación distinta para aplicar una idea del material."
        )
        _reject_chat_markers(content)
        instructions = REINFORCEMENT_INSTRUCTIONS
        for _ in range(2):
            ids = _chat_ids(self.tokenizer, instructions, content)
            if len(ids) + REINFORCEMENT_TOKENS > self.model.config.max_position_embeddings:
                raise ExampleTooLong("La fuente y el intento exceden el contexto disponible para esta actividad.")
            text = self._generate(ids, REINFORCEMENT_TOKENS, use_adapter=False).strip()
            reason = _rejection_reason(text, question)
            if reason is None:
                return text
            instructions = (
                REINFORCEMENT_INSTRUCTIONS + "\nCorrección obligatoria: " + reason + " "
                "Crea otra situación y pregunta por una consecuencia del cambio. "
                "No copies ninguna parte de la pregunta original. Escribe solo el nuevo enunciado."
            )

        raise ValueError(
            "No pudimos crear un refuerzo diferente sin repetir la pregunta ni mostrar su solución. "
            "Vuelve a generar el ejercicio. Tu intento quedó guardado."
        )
