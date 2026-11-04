ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}
COPY wheels /tmp/wheels
RUN python -m pip install --no-index --find-links=/tmp/wheels --no-cache-dir --no-compile numpy==1.26.4 scipy==1.13.1 h5py==3.11.0 matplotlib==3.9.0 sympy==1.12.1 \
 && rm -rf /tmp/wheels \
 && mkdir -p /workspace /grader && chown 65534:65534 /workspace /grader
ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 PIP_NO_INDEX=1 OPENBLAS_NUM_THREADS=1 MPLCONFIGDIR=/tmp/matplotlib
USER 65534:65534
WORKDIR /workspace
