FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cos_ui.py hive_management.py ./
COPY cosmanagement ./cosmanagement

# Configuration (config.json, hive.json, .env, storage_locations.txt, CA .pem) is mounted at /data, never baked in.
RUN useradd --system --no-create-home cos && mkdir /data && chown cos /data
USER cos
WORKDIR /data

EXPOSE 8767
# --bind 0.0.0.0 is the container's own interface; compose publishes the port on the host's 127.0.0.1 only.
ENTRYPOINT ["python", "/app/cos_ui.py", "--bind", "0.0.0.0", "--port", "8767", "--no-browser"]
