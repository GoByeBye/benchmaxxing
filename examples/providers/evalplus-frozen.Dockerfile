# Explicit preparation only. Pin the resulting immutable image ID before runs.
FROM python:3.11-slim
RUN python -m pip install --no-cache-dir numpy==1.26.4 \
    && mkdir -p /workspace /grader /tmp/benchmaxxing-grade \
    && chown -R 65534:65534 /workspace /grader /tmp/benchmaxxing-grade
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 PIP_NO_INDEX=1
USER 65534:65534
WORKDIR /workspace
CMD ["python", "-u", "-c", "import time; time.sleep(86400)"]
