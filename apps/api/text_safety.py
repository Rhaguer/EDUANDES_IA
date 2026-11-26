"""Zero-dependency text safety helpers shared by local and cloud engines."""

from __future__ import annotations

RESERVED_TEXT = ("<|im_start|>", "<|im_end|>", "<|endoftext|>", "<think>", "</think>")


class ExampleTooLong(ValueError):
    """The evidence or target cannot fit without losing text."""


def reject_chat_markers(text: str) -> None:
    if any(marker in text for marker in RESERVED_TEXT):
        raise ValueError("La entrada contiene un marcador reservado del modelo.")
