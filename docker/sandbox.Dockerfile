ARG PYTHON_IMAGE=python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
FROM ${PYTHON_IMAGE}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/codepilot
COPY docker/sandbox-requirements.txt /opt/codepilot/sandbox-requirements.txt
RUN pip install --no-cache-dir --require-hashes -r sandbox-requirements.txt

COPY src/codepilot /opt/codepilot/codepilot
ENV PYTHONPATH=/opt/codepilot

WORKDIR /workspace
USER 65532:65532
CMD ["python", "-c", "import time; time.sleep(2147483647)"]
