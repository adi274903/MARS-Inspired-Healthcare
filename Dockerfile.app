# Code-only layer on top of the heavy base image (deps + embedding model + data, built once from ./Dockerfile and
# tagged `base` in Artifact Registry). Rebuilding after a code change uploads kilobytes, not 1.7 GB.
ARG BASE
FROM ${BASE}

COPY requirements-otel.txt /tmp/requirements-otel.txt
RUN pip install -r /tmp/requirements-otel.txt

# Context excludes data/ and models/ (.gcloudignore.app); those already live in the base image.
COPY . .
