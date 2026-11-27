FROM python:3.12-slim AS builder

WORKDIR /build
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
COPY requirements-aws.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-aws.txt
COPY apps/native/eduandes_core.cpp apps/native/eduandes_core.cpp
RUN g++ -O2 -s -std=c++20 -Wall -Wextra -Werror -fPIC -shared \
    apps/native/eduandes_core.cpp -o apps/native/eduandes_core.so

FROM python:3.12-slim

WORKDIR /app
COPY --from=builder /install /usr/local
COPY apps/api ./apps/api
COPY apps/web/dist ./apps/web/dist
COPY --from=builder /build/apps/native/eduandes_core.so ./apps/native/eduandes_core.so
COPY scripts ./scripts
RUN useradd -u 1000 -m -s /bin/sh eduandes \
    && mkdir -p /app/data /app/artifacts \
    && chown -R eduandes:eduandes /app

USER eduandes

ENV PYTHONUNBUFFERED=1
ENV PYTHONUTF8=1
ENV EDUCORE_ENGINE=openai_compat
ENV EDUCORE_DB=/app/data/educore.sqlite3
ENV PORT=8000

EXPOSE 8000
CMD ["sh", "-c", "python scripts/laptop/servir.py --host 0.0.0.0 --port ${PORT:-8000}"]
