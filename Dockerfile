FROM python:3.11.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY settler/ /app/settler/

# /data holds the persisted API key digests; mount a volume here
RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir /data && chown app /data
USER 10001
VOLUME ["/data"]

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=3).status != 200)"

# Single process only: the settlement nonce lock is process-local
ENTRYPOINT ["uvicorn", "settler.main:app", "--host", "0.0.0.0", "--port", "8002", "--workers", "1"]
