FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 carmen

COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
COPY migrations ./migrations
COPY configs/collector-output.schema.json ./configs/collector-output.schema.json

RUN pip install --no-cache-dir ".[hosted]"

USER carmen

EXPOSE 8080

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 90 carmen_agent.hosted.app:application"]
