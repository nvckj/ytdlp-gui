FROM python:3.12-slim

# ffmpeg is required for MP3 audio extraction
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static

ENV DOWNLOAD_DIR=/data PORT=${PORT:-8000}
VOLUME /data
EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app:app --host 0.0.0.0 --port $PORT"]
