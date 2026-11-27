"""Text safety helpers accelerated by the C++20 native core with Python fallback."""

from __future__ import annotations

from .native_core import has_reserved_markers

RESERVED_TEXT = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<think>", "</think>")


class ExampleTooLong(ValueError):
    """The evidence or target cannot fit without losing text."""


def reject_chat_markers(text: str) -> None:
    if has_reserved_markers(text):
        raise ValueError("La entrada contiene un marcador reservado del modelo.")
