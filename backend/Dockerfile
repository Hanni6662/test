ARG PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12.15-slim-bookworm
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.lock /tmp/requirements.lock
COPY requirements-dev.lock /tmp/requirements-dev.lock
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then \
      python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-dev.lock; \
    else \
      python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock; \
    fi \
    && rm /tmp/requirements.lock /tmp/requirements-dev.lock

COPY pyproject.toml alembic.ini ./
COPY app ./app
COPY migrations ./migrations
COPY tests ./tests
COPY scripts/check_task_contract.py ./scripts/check_task_contract.py
COPY scripts/test-entrypoint.sh ./scripts/test-entrypoint.sh

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && chmod 0555 /app/scripts/test-entrypoint.sh

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
