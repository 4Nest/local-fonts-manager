ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FONT_MANAGER_ROOT=/fonts FONT_MANAGER_STATE=/state \
    FONT_MANAGER_HOST=0.0.0.0 FONT_MANAGER_PORT=8765 TZ=Asia/Shanghai
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
       tesseract-ocr-chi-tra tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.10.2
COPY pyproject.toml uv.lock ./
COPY font_manager ./font_manager
RUN uv sync --locked --no-dev --no-editable --python /usr/local/bin/python \
    && rm -rf /root/.cache/uv \
    && mkdir -p /state && chown 1000:1001 /state
USER 1000:1001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/auth', timeout=3)"]
ENTRYPOINT ["/app/.venv/bin/python", "-m", "font_manager"]
CMD ["web"]

