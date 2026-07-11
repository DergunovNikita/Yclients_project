FROM python:3.12-slim
#checkov:skip=CKV_DOCKER_2:Healthchecks are configured at service level because this image also runs worker, migrate, and tool commands
#checkov:skip=CKV_DOCKER_3:Container user is controlled by the deployment runtime to preserve mounted log volume permissions

ARG APP_REVISION=local

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

LABEL org.opencontainers.image.revision=$APP_REVISION

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN printf '%s\n' "$APP_REVISION" > /app/REVISION
RUN chmod +x /app/docker/entrypoint.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["api"]
