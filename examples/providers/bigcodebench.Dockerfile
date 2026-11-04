FROM bigcodebench/bigcodebench-evaluate@sha256:1327bddf60be9bc241648c59e6060cac4ca50248a0588ab735cd0200b17cc8c2
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip install --no-cache-dir --no-deps --force-reinstall git+https://github.com/bigcode-project/bigcodebench.git@09dd993f46c3fbf3a799465bb96d524edcb0b199 \
 && mkdir -p /workspace /grader && chown 65534:65534 /workspace /grader
ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 PIP_NO_INDEX=1 OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/matplotlib
USER 65534:65534
WORKDIR /workspace
