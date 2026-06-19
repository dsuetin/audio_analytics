FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# системные зависимости (librosa, scipy, soundfile)
RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libsndfile1 \
    git \
    && rm -rf /var/lib/apt/lists/*

# upgrade pip
RUN pip install --no-cache-dir -U pip setuptools wheel

# copy requirements first
COPY asr_worker/requirements.txt /app/requirements.txt

# install python deps
RUN pip install --no-cache-dir -r requirements.txt

# copy code
COPY asr_worker/ /app/asr_worker/

CMD ["python", "-m", "asr_worker.main"]