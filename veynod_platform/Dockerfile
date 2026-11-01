FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY veynod_platform/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY veynod_platform /app/veynod_platform
RUN mkdir -p /data
ENV PLATFORM_DB_PATH=/data/platform.db
EXPOSE 8010
CMD ["python", "-m", "uvicorn", "veynod_platform.app:app", "--host", "0.0.0.0", "--port", "8010"]
