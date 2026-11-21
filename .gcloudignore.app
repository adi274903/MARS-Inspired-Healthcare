# Code-only upload (Dockerfile.app): the data and the model are already in the base image. Unlike .gitignore, data/papers.db, data/qdrant and models/ ARE uploaded
# (they go into the private image only, never to GitHub).
.gcloudignore
.git
.github
.venv
.env
.env.*
**/__pycache__
**/*.pyc
.pytest_cache
azure/
infra/
deck/
demo/
architecture/
tests/
final_merged_dataset.xlsx
data/neo4j/
data/qdrant_prev/
data/qdrant_next/
data/papers.jsonl
data/models.db
events/*.db
data/
models/
Dockerfile
