FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY settler/ /app/settler/

EXPOSE 8002

ENTRYPOINT ["uvicorn", "settler.main:app", "--host", "0.0.0.0", "--port", "8002"]
