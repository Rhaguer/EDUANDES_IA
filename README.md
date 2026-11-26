# EduAndes AI

Plataforma educativa de asistencia docente y refuerzo personalizado alineada al **ODS 4 (Educación de calidad)**.

EduAndes AI transforma material autorizado del docente (PDF o texto) en borradores de preguntas revisables con **citas literales verificadas** y ofrece **refuerzos personalizados** vinculados al error del estudiante. La IA propone y el docente decide: ninguna actividad se publica sin revisión y respaldo documental comprobado.

## Equipo Binacional (Chile – Ecuador)

- **Ricardo** (Chile)
- **Pablo** (Ecuador)

## Arquitectura

- **Frontend:** Web compilada en `apps/web/dist` (React).
- **Backend / API:** FastAPI + SQLite (`apps/api/main.py`, `apps/api/courses.py`).
- **Verificación documental:** Filtros deterministas que exigen cita literal exacta en la página del material (`apps/api/course_generation.py`, `apps/api/reinforcement_generation.py`).
- **Motor IA (`educore_ml`):** `Qwen/Qwen3-1.7B` con adaptador LoRA entrenado sobre el corpus en español SQAC (`artifacts/main-01/adapter/`).

## Despliegue ultraliviano en AWS (Ubuntu 1 GB RAM)

Para correr en una instancia EC2 `t2.micro` / `t3.micro` (1 GB RAM, sin GPU ni PyTorch):

```bash
git clone https://github.com/Rhaguer/EDUANDES_IA.git && cd EDUANDES_IA
export EDUCORE_ENGINE=bedrock
export AWS_REGION=us-east-1
export EDUCORE_BEDROCK_MODEL_ID=us.amazon.nova-lite-v1:0
# Opcional: respaldo automático de PDFs en S3
# export EDUCORE_S3_BUCKET=mi-bucket-eduandes
bash scripts/aws/run_ubuntu.sh
```

Si Amazon Bedrock no estuviera habilitado en tu cuenta, puedes usar cualquier proveedor compatible con OpenAI (por ejemplo Groq o Gemini) sin instalar PyTorch:

```bash
export EDUCORE_ENGINE=openai_compat
export EDUCORE_LLM_BASE_URL=https://api.groq.com/openai/v1
export EDUCORE_LLM_API_KEY=tu_api_key
export EDUCORE_LLM_MODEL=llama-3.1-8b-instant
bash scripts/aws/run_ubuntu.sh
```

También incluye `Dockerfile` listo para **AWS App Runner**.

## Arranque local (con GPU NVIDIA)

1. Crear un entorno virtual con Python 3.12 e instalar dependencias:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[app]"
   ```
2. Levantar el servidor unificado (API + web en un solo puerto):
   ```powershell
   .\.venv\Scripts\python.exe scripts\laptop\servir.py --host 127.0.0.1 --port 8765
   ```
3. Abrir `http://127.0.0.1:8765/#/` en el navegador. La base SQLite (`data/educore.sqlite3`) se inicializa vacía en el primer arranque para cargar asignaturas, unidades y materiales propios.
