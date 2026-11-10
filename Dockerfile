FROM python:3.12-slim

# Unbuffered output so logs show up in Railway immediately
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# gcc / build-essential are needed to compile TgCrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The echo is a diagnostic: if CONTAINER_STARTED never appears in Railway's
# Deploy Logs, the problem is Railway config/deploy, not the Python code.
CMD ["sh", "-c", "echo CONTAINER_STARTED && exec python -u main.py"]
