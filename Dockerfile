FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV MOB_HOST=0.0.0.0 MOB_PORT=8080 MOB_DB=/data/mobilite.db PYTHONUNBUFFERED=1
RUN mkdir /data && chown 10001 /data
VOLUME /data
USER 10001
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
CMD ["python", "server.py"]
