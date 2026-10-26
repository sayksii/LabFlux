# Shared LabFlux job image: Python 3.12 + uv. Jobs bring pyproject.toml and uv.lock.
# Build: docker build -t localhost/labflux/python-uv:3.12-uv0.11.8 images/python-uv
FROM ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 AS uv

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
COPY --from=uv /uv /uvx /usr/local/bin/
# Jobs run as the Worker's UID without a passwd entry, where HOME would be the unwritable /.
# The uv cache and the workspace are separate bind mounts, so uv cannot hardlink between them.
ENV HOME=/tmp \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
