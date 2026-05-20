FROM python:3.11-slim

# System deps:
#  - ffmpeg: required by yt-dlp to merge separate video+audio streams.
#  - libgl1, libglib2.0-0: needed by opencv-python's runtime.
#  - curl: lets us pre-fetch the MediaPipe pose model into the image so we
#    don't re-download it on every container start.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so layer caching keeps app code edits fast.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the MediaPipe pose model into the image (~30 MB, full variant).
RUN mkdir -p /root/.cache/mediapipe-models \
 && curl -sSL -o /root/.cache/mediapipe-models/pose_landmarker_full.task \
        https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task

COPY . .

# Fly mounts a persistent volume at /data so library + videos survive deploys.
# main.py resolves paths relative to its own directory, so we symlink into it.
RUN rm -rf /app/data /app/downloads \
 && mkdir -p /persistent/data /persistent/downloads \
 && ln -s /persistent/data /app/data \
 && ln -s /persistent/downloads /app/downloads

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
