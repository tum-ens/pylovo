# pylovo + its headless HTTP API pylovo-api in one image (the browser UI lives in GridPlanner).
# Build from the repository root:
#   docker build -f api/docker/Dockerfile --build-arg PYLOVO_REVISION=$(git rev-parse HEAD) -t pylovo .
# The database is not part of the image: point the mounted .env to your InfDB/PostgreSQL, or pass
# DBNAME, DBUSER, PASSWORD, HOST, PORT (USE_INFDB, INFDB_*_SCHEMA, TARGET_EPSG) as environment
# variables (GridPlanner does). CI publishes ghcr.io/tum-ens/pylovo (.github/workflows/pylovo-image.yml).
FROM python:3.12-slim

# ogr2ogr (GDAL) is used by pylovo-setup to load the transformer GeoJSON.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never

# The server and its jobs run as an unprivileged user. Id 1000 is the first user of most Linux hosts, so
# the mounted config/, data/, log/ and .pylovo-api/ stay writable; for another owner use `user:` in
# compose or `docker run --user`.
RUN useradd --create-home --uid 1000 pylovo

WORKDIR /app
# Dependencies first (cached layer), then the project itself. The uv download cache is a build
# cache mount, so it stays out of the image. Not installed: the dev group (pytest) and the plots
# extra (matplotlib, plotly, ...): no module of the API or of its job commands imports them
# (api/tests/test_image_files.py).
COPY pyproject.toml uv.lock README.rst .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --extra api
COPY --chown=pylovo:pylovo . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra api
# Jobs write results/ below /app; the state folder and log/ may be missing when nothing is mounted there.
RUN mkdir -p /app/.pylovo-api /app/log && chown pylovo:pylovo /app /app/.pylovo-api /app/log

# Git commit reported by GET /api/health (.git is not copied into the image).
ARG PYLOVO_REVISION=""
ENV PATH="/app/.venv/bin:${PATH}" PYLOVO_ROOT=/app PYLOVO_REVISION=${PYLOVO_REVISION}
EXPOSE 18765
USER pylovo
# Checks the port of the default command; a container started with another --port needs its own check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18765/api/health', timeout=4)"]
# Binds 127.0.0.1: run the container with host networking (see compose.yaml) so the API stays local.
CMD ["pylovo-api", "--host", "127.0.0.1", "--port", "18765"]
