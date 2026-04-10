FROM docker.io/python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir alpaca-mcp-server==2.0.0
ENTRYPOINT ["alpaca-mcp-server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
