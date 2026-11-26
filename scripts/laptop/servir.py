"""EduCore AI en UN proceso y UN puerto: la API FastAPI y la web ya compilada (apps/web/dist).

No necesita Node.js. La web usa rutas con # (http://127.0.0.1:8765/#/...), así que el servidor
solo entrega "/" (index.html) y "/assets/*". Todo lo que empieza con /api lo atienden las rutas
de la API, porque se registran antes de montar la web.

Uso (desde la raíz del proyecto o del kit, con las variables EDUCORE_* ya definidas):
    .venv\\Scripts\\python.exe scripts\\laptop\\servir.py --port 8765

Variables que lee:
    EDUCORE_DB            base SQLite (los materiales van en <stem de la base>-materials, al lado)
    EDUCORE_ADAPTER_DIR   adaptador LoRA (solo se carga al generar)
    EDUCORE_WEB_DIST      carpeta de la web compilada (por defecto apps/web/dist)
    EDUCORE_PORT          puerto por defecto si no se pasa --port
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("USE_TORCH", "1")

ROOT = Path(__file__).resolve().parents[2]
# main.py usa rutas relativas (.cache/huggingface, data/educore.sqlite3, artifacts/...).
os.chdir(ROOT)
for entry in (ROOT / "src", ROOT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from fastapi.staticfiles import StaticFiles  # noqa: E402

from apps.api.main import app  # noqa: E402  (lee EDUCORE_DB y EDUCORE_ADAPTER_DIR al importar)

DIST = Path(os.environ.get("EDUCORE_WEB_DIST") or ROOT / "apps" / "web" / "dist").resolve()
if not (DIST / "index.html").is_file():
    raise SystemExit(f"No encuentro la web compilada en {DIST} (falta index.html).")

# Se monta al final: las rutas /api/... ya registradas tienen prioridad.
app.mount("/", StaticFiles(directory=DIST, html=True), name="web")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sirve la API y la web de EduCore en un solo puerto.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("EDUCORE_PORT", "8765")))
    args = parser.parse_args()

    import uvicorn

    print(f"EduCore: API + web en http://{args.host}:{args.port}/#/ (web: {DIST})", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
