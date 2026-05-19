FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        default-libmysqlclient-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# /app is the Python root; the hub package lives at /app/hub/
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy hub source into /app/hub/ so `import hub` works
COPY . ./hub/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# --workers=1 keeps the background threads from duplicating (see wsgi.py)
CMD ["gunicorn", "hub.wsgi:app", \
     "--workers=1", "--threads=4", \
     "--bind=0.0.0.0:5000", \
     "--access-logfile=-", "--error-logfile=-", \
     "--log-level=info", "--timeout=120"]
