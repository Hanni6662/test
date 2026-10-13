ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY image_service ./image_service
RUN useradd --uid 10001 --create-home imageworker && mkdir -p /data && chown imageworker:imageworker /data
USER imageworker
ENV IMAGE_SERVICE_DATA_DIR=/data
EXPOSE 8091
CMD ["uvicorn", "image_service.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--workers", "1", "--no-access-log"]
