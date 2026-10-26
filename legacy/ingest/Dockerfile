FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
RUN pip install --no-cache-dir fonttools==4.62.1 Pillow==11.3.0
COPY --chmod=0644 font_ingest.py classification.py metadata.py health.py test_ingest.py ./
COPY --chmod=0644 参考站目录.txt ./参考站目录.txt
COPY --chmod=0644 config.nas.json ./config.json
USER 1000:1001
ENTRYPOINT ["python", "-X", "utf8", "/app/font_ingest.py"]
CMD ["watch", "--config", "/app/config.json", "--state", "/state"]
