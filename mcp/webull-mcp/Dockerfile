FROM docker.io/python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# /app/conf holds the OAuth token — mount a named volume here so it persists across restarts
VOLUME ["/app/conf"]
COPY run_http.py .
CMD ["python", "run_http.py"]
