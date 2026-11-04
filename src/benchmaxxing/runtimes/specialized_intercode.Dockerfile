# Preparation-only InterCode CTF tool profile. Task data is staged per attempt.
# Do not COPY the upstream corpus: it includes solution directories.
FROM public.ecr.aws/docker/library/ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash python3 python3-pip python3-pycryptodome psmisc bsdmainutils cron \
    imagemagick dnsutils git wget gcc sleuthkit netcat-openbsd unzip curl gdb poppler-utils \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/local/bin/python \
    && mkdir -p /workspace /grader && chmod 1777 /workspace /grader
WORKDIR /workspace
USER 65534:65534
CMD ["sleep", "infinity"]
