# One image for every role. Cloud Run picks the entrypoint per resource:
#   API        milos serve api            (MILOS_API_ROLE=public|internal)
#   connector  milos serve connector --name internal|egress
#   runner     python -m milos.runner
#
# The console frontend is built here so its bundle need not be committed; the
# public API serves it beside /v1 (src/milos/console).
FROM node:22-slim AS console
WORKDIR /console
COPY console/package.json console/package-lock.json /console/
RUN npm ci
COPY console /console
RUN npx next build

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE /app/
RUN uv sync --frozen --no-dev --no-install-project

COPY src /app/src
COPY --from=console /console/out /app/src/milos/console/static
RUN uv sync --frozen --no-dev

# The runner works as an unprivileged user in /work; the SDK keeps its
# transcript under $HOME/.claude, which the snapshot includes.
RUN useradd --create-home --uid 1001 sandbox && mkdir -p /work && chown sandbox /work
USER sandbox
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/home/sandbox \
    MILOS_WORK_DIR=/work

ENTRYPOINT ["milos"]
CMD ["serve", "api"]
