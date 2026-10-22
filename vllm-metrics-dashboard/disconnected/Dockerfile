FROM python:3.12.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INDEX=1

WORKDIR /opt/vllm-metrics-dashboard

COPY backend/requirements.lock ./requirements.lock
COPY disconnected/python-wheelhouse/ /wheels/
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock \
    && rm -rf /wheels

COPY backend/app ./backend/app
COPY frontend/dist ./frontend/dist

EXPOSE 8000
USER 1001
CMD ["uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
