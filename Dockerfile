FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIS_DB_PATH=/data/gis.db \
    GIS_BUSY_TIMEOUT_S=30

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY examples ./examples

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin gis \
    && mkdir -p /data \
    && chown gis:gis /data
USER gis

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python app/healthcheck.py

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
