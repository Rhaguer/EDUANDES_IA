FROM python:3.12-slim

WORKDIR /app
COPY requirements-aws.txt .
RUN pip install --no-cache-dir -r requirements-aws.txt

COPY apps ./apps
COPY scripts ./scripts
RUN mkdir -p /app/data /app/artifacts

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1
ENV EDUCORE_ENGINE=bedrock
ENV EDUCORE_DB=/app/data/educore.sqlite3
ENV PORT=8000

EXPOSE 8000
CMD ["sh", "-c", "python scripts/laptop/servir.py --host 0.0.0.0 --port ${PORT:-8000}"]
