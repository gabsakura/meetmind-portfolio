FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN groupadd --system meetmind && useradd --system --gid meetmind --home /app meetmind
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY config.py run.py ./
COPY docker-entrypoint.sh ./
RUN mkdir -p /data/submissions && \
    chmod +x /app/docker-entrypoint.sh && \
    chown -R meetmind:meetmind /app /data/submissions
USER meetmind
EXPOSE 8000
CMD ["/app/docker-entrypoint.sh"]
