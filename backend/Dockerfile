FROM python:3.11-slim

ENV POETRY_VERSION=2.3.3 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock ./
RUN poetry install --only main --no-root --no-interaction --no-ansi

COPY health_assistant ./health_assistant
COPY _griffe ./_griffe

EXPOSE 8000

CMD ["uvicorn", "health_assistant.main:app", "--host", "0.0.0.0", "--port", "8000"]
