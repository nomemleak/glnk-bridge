FROM python:3.13-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY glnk_bridge ./glnk_bridge
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME /data
CMD ["python", "-m", "glnk_bridge"]
