FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth=1 https://github.com/bidouilles/mcp-tradingview-server.git . && \
    uv pip install --system -e .

# Shim for pkg_resources.resource_filename (removed in modern setuptools on this base image)
RUN python -c "import importlib.resources, pathlib; print('ok')" && \
    printf 'import importlib.resources as _ir, pathlib as _p\ndef resource_filename(pkg, path):\n    try:\n        ref = _ir.files(pkg).joinpath(path)\n        return str(ref)\n    except Exception:\n        import site, os\n        for d in site.getsitepackages():\n            full = _p.Path(d) / pkg.replace(".", "/") / path\n            if full.exists():\n                return str(full)\n        return path\n' > /usr/local/lib/python3.12/site-packages/pkg_resources.py

EXPOSE 8000

CMD ["python", "/app/tradingview_server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
