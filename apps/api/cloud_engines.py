"""Ultra-lightweight cloud AI engines (AWS Bedrock / OpenAI-compatible) and S3 backup.

Designed for 1 GB RAM Ubuntu EC2 instances without PyTorch or Transformers installed.
"""

from __future__ import annotations

import json
import logging
import os
from threading import Lock
import urllib.error
import urllib.request

from .text_safety import reject_chat_markers as _reject_chat_markers

LOGGER = logging.getLogger(__name__)


def backup_material_to_s3(filename: str, data: bytes, material_id: str) -> str | None:
    """Upload a copy of an uploaded material to Amazon S3 when EDUCORE_S3_BUCKET is configured."""
    bucket = os.environ.get("EDUCORE_S3_BUCKET", "").strip()
    if not bucket:
        return None
    import boto3

    region = os.environ.get("EDUCORE_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    prefix = os.environ.get("EDUCORE_S3_PREFIX", "materials").strip("/")
    key = f"{prefix}/{material_id}/{filename}" if prefix else f"{material_id}/{filename}"
    client = boto3.client("s3", region_name=region)
    client.put_object(Bucket=bucket, Key=key, Body=data)
    return f"s3://{bucket}/{key}"


class _CloudEngineBase:
    """Shared course item and reinforcement entrypoints for lightweight API-backed engines."""

    reinforcement_prompt_version = "cloud-v1"
    parallel_safe = True  # API-backed: many questions can be requested concurrently

    def __init__(self) -> None:
        self._lock = Lock()

    def generate_course_item(
        self,
        page: dict,
        objective: str,
        item_type: str,
        previous_questions: list[str],
        *,
        previous_answers: list[str] | None = None,
        style_index: int = 0,
    ) -> dict:
        from .course_generation import generate_item

        return generate_item(
            self,
            page,
            objective,
            item_type,
            previous_questions,
            previous_answers=previous_answers,
            style_index=style_index,
        )

    def generate_course_reinforcement(self, attempt: dict, source_page: dict) -> dict:
        from .reinforcement_generation import generate_reinforcement

        return generate_reinforcement(self, attempt, source_page)

    def complete_structured(
        self,
        instructions: str,
        content: str,
        *,
        creative: bool = False,
        seed: int = 0,
        tokens: int = 750,
    ) -> str:
        return self.course_completion(instructions, content, creative=creative, seed=seed, tokens=tokens)

    def generate(
        self,
        context: str,
        answer: str,
        *,
        max_new_tokens: int = 96,
        use_adapter: bool = True,
        answer_start: int | None = None,
    ) -> str:
        instructions = (
            "Redacta exactamente UNA pregunta abierta breve en español usando únicamente el contexto. "
            "La pregunta debe poder responderse con la respuesta objetivo, sin revelar la respuesta en el enunciado. "
            "Devuelve únicamente la pregunta."
        )
        content = f"Contexto:\n{context[:4000]}\n\nRespuesta objetivo:\n{answer}"
        return self.course_completion(instructions, content, creative=False, seed=42, tokens=max_new_tokens)

    def guidance(self, context: str, question: str, reference: str, attempt: str, *, practice: bool = False) -> str:
        if practice:
            instructions = (
                "Propón un ejercicio breve de refuerzo diferente en español usando el material, "
                "sin repetir la pregunta original ni dar la solución."
            )
        else:
            instructions = (
                "Explica en español la respuesta de referencia en 2 o 3 frases usando el material "
                "e invita a contrastar con el intento del estudiante."
            )
        content = (
            f"Contexto:\n{context[:4000]}\n\nPregunta:\n{question}\n\n"
            f"Referencia:\n{reference}\n\nIntento del estudiante:\n{attempt}"
        )
        return self.course_completion(instructions, content, creative=practice, seed=42, tokens=180)


class BedrockEngine(_CloudEngineBase):
    """Amazon Bedrock engine via boto3 Converse API (0 MB PyTorch/GPU memory)."""

    def __init__(self, model_id: str | None = None, region: str | None = None):
        super().__init__()
        import boto3

        self.region = region or os.environ.get("EDUCORE_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
        self.model_id = (
            model_id
            or os.environ.get("EDUCORE_BEDROCK_MODEL_ID")
            or "us.amazon.nova-lite-v1:0"
        )
        from botocore.config import Config

        self.client = boto3.client(
            "bedrock-runtime",
            region_name=self.region,
            config=Config(
                retries={"max_attempts": 6, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=40,
                max_pool_connections=32,
            ),
        )

    def status(self) -> dict:
        return {
            "base_model": self.model_id,
            "base_revision": self.region,
            "adapter_active": False,
            "adapter_version": None,
            "profile": "aws-bedrock",
            "precision": "cloud",
            "device": f"bedrock:{self.region}",
            "thinking": False,
            "mode": "cloud-bedrock",
            "reinforcement_choices": "generated",
        }

    def course_completion(self, instructions: str, content: str, *, creative: bool, seed: int, tokens: int) -> str:
        _reject_chat_markers(instructions)
        _reject_chat_markers(content)
        inference_config = {"maxTokens": int(tokens), "temperature": 0.65 if creative else 0.0}
        if creative:
            inference_config["topP"] = 0.9
        # boto3 clients are thread-safe: no lock, so several questions are generated at once.
        response = self.client.converse(
            modelId=self.model_id,
            system=[{"text": instructions}],
            messages=[{"role": "user", "content": [{"text": content}]}],
            inferenceConfig=inference_config,
        )
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in blocks if isinstance(block, dict)).strip()


class OpenAICompatibleEngine(_CloudEngineBase):
    """Zero-dependency HTTP client for OpenAI-compatible endpoints (Groq, Gemini OpenAI compat, OpenAI)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ):
        super().__init__()
        self.base_url = (base_url or os.environ.get("EDUCORE_LLM_BASE_URL") or "https://api.groq.com/openai/v1").rstrip("/")
        self.api_key = api_key or os.environ.get("EDUCORE_LLM_API_KEY", "")
        self.model = model or os.environ.get("EDUCORE_LLM_MODEL") or "llama-3.1-8b-instant"
        if not self.api_key:
            raise RuntimeError("Falta EDUCORE_LLM_API_KEY para el motor cloud HTTP.")

    def status(self) -> dict:
        return {
            "base_model": self.model,
            "base_revision": "api",
            "adapter_active": False,
            "adapter_version": None,
            "profile": "cloud-http",
            "precision": "cloud",
            "device": "cloud-api",
            "thinking": False,
            "mode": "cloud-http",
            "reinforcement_choices": "generated",
        }

    def course_completion(self, instructions: str, content: str, *, creative: bool, seed: int, tokens: int) -> str:
        _reject_chat_markers(instructions)
        _reject_chat_markers(content)
        payload = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": content},
            ],
            "max_tokens": int(tokens),
            "temperature": 0.65 if creative else 0.0,
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:300]
            raise RuntimeError(f"Error HTTP {exc.code} del proveedor LLM: {detail}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("El proveedor LLM no devolvió respuesta.")
        return str((choices[0].get("message") or {}).get("content") or "").strip()
