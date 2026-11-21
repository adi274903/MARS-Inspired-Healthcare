# Code-only layer on top of the heavy base image (deps + embedding model + data, built once from ./Dockerfile and
# tagged `base` in Artifact Registry). Rebuilding after a code change uploads kilobytes, not 1.7 GB.
ARG BASE
FROM ${BASE}

COPY requirements-otel.txt /tmp/requirements-otel.txt
RUN pip install -r /tmp/requirements-otel.txt

# Context excludes data/ and models/ (.gcloudignore.app); those already live in the base image.
COPY . .

# Run as an unprivileged user. Only what the app must write is owned by it: the embedded Qdrant store (lock files),
# the SQLite directory (journal files), and a home for library caches. Code and the model stay root-owned, read-only.
RUN useradd --create-home --uid 10001 app \
 && chown app /app/data \
 && chown -R app /app/data/qdrant \
 && chown app /app/data/papers.db
USER app
