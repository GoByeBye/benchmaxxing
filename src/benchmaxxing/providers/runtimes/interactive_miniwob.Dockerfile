# Explicit preparation only. Supply a previously inspected digest-pinned base.
ARG BASE_IMAGE=python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
FROM ${BASE_IMAGE} AS wheels
COPY interactive_miniwob.lock /requirements.lock
RUN python -m pip download --only-binary=:all: --require-hashes \
    --dest /wheelhouse -r /requirements.lock

FROM ${BASE_IMAGE}
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=wheels /wheelhouse /wheelhouse
COPY --from=wheels /requirements.lock /opt/benchmaxxing/requirements.lock
RUN python -m pip install --no-cache-dir --no-index --require-hashes \
    --find-links=/wheelhouse -r /opt/benchmaxxing/requirements.lock \
    && rm -rf /wheelhouse
RUN python -m playwright install --with-deps chromium \
    && dpkg-query -W > /opt/benchmaxxing/system-packages.lock \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /opt/playwright
RUN python -c "import browsergym.miniwob; import playwright.sync_api" \
    && mkdir -p /workspace /grader && chmod 1777 /workspace /grader
LABEL org.benchmaxxing.runtime="browsergym-miniwob/0.14.3"
WORKDIR /tmp
