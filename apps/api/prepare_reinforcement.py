"""Download the pinned optional reinforcement model; never runs at API startup."""
from pathlib import Path
import os


def main():
    os.environ.setdefault("HF_HOME", str(Path(".cache/huggingface").resolve()))
    from huggingface_hub import snapshot_download
    from .reinforcement_engine import BASE_MODEL, BASE_REVISION

    location = snapshot_download(
        BASE_MODEL, revision=BASE_REVISION, token=False,
        allow_patterns=["*.json", "*.safetensors", "*.jinja", "*.txt"], max_workers=3,
    )
    print(f"Modelo local de refuerzos preparado en: {location}")


if __name__ == "__main__":
    main()
