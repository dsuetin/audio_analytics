FROM python:3.10-slim

# -------------------------
# system deps
# -------------------------
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# -------------------------
# workdir
# -------------------------
WORKDIR /app

# -------------------------
# python deps
# -------------------------
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# -------------------------
# source code
# -------------------------
COPY asr_worker/ ./asr_worker/

# -------------------------
# env
# -------------------------
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# -------------------------
# run
# -------------------------
	
CMD ["python", "-m", "asr_worker.main"]