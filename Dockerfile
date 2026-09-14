# syntax=docker/dockerfile:1
#
# Migration insurance, not a local workflow - see README.md. This
# proves the project can be built and run as a container on Linux; it
# is built and exercised in CI (.github/workflows/ci.yml), but nobody
# is expected to use it day to day.
#
# Based on Astral's official uv + Docker example.
FROM ghcr.io/astral-sh/uv:python3.12-trixie-slim

# Run as a non-root user rather than the image's default root.
RUN groupadd --system --gid 999 nonroot \
    && useradd --system --gid 999 --uid 999 --create-home nonroot

WORKDIR /app

# Keeps Python from buffering output, so logs show up immediately
# rather than only once a buffer fills.
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
# Dev tools (pytest, ruff, pre-commit) have no place in this image -
# they're for development, not for whatever eventually runs this.
ENV UV_NO_DEV=1

# Install dependencies first, from the lockfile only - this layer only
# rebuilds when pyproject.toml/uv.lock actually change, not on every
# source edit.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

# Now add the rest of the project and install it.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT []
USER nonroot

# There is nothing to run automatically here: a real scan needs secrets
# (a Gmail app password, an ntfy.sh topic, a monitoring URL) that only
# ever live outside this image, in the private environment file
# described in README.md - never baked in here. This default command
# is just a harmless confirmation that the image works.
CMD ["python", "-c", "print('vpa-scanner image built OK. See README.md for how this would actually be run.')"]
