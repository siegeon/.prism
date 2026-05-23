# ---- SPA build ----------------------------------------------------------
FROM node:22-slim AS web-build
WORKDIR /web
COPY prism_service/web/package.json prism_service/web/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY prism_service/web/ ./
RUN npm run build

# ---- Service ------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /srv

# System deps:
#   * gcc          — SQLite FTS5 build
#   * git          — source_service.ensure_cloned (v5.1+)
#   * curl, ca-certificates — node setup script needs them
#   * node + npm   — required by `@anthropic-ai/claude-code` (the `claude`
#                    CLI is a Node app), which the v5.1.6 understand_drainer
#                    invokes per-job. NodeSource provides a slim Node 22 LTS
#                    on Debian bookworm that won't drag in extra runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc git curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g --no-audit --no-fund @anthropic-ai/claude-code \
    && apt-get remove -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Sanity-check the claude binary landed on PATH so a broken image fails
# at build time, not when the drainer first tries to run.
RUN claude --version

# v5.3.0: install via pyproject so docker matches `pip install prism-service`.
# Copy the package source + pyproject.toml + README so setuptools resolves
# the dynamic version attr and reads the long_description from README.
COPY pyproject.toml README.md* ./
COPY prism_service/ ./prism_service/
# SPA bundle from the web-build stage (above) lands at prism_service/web_dist/
# *before* `pip install .` so the package_data glob picks it up at wheel time.
COPY --from=web-build /web_dist /srv/prism_service/web_dist
# Torch CPU-only wheels go to a separate index; install them first so the
# subsequent `pip install .` doesn't drag in the CUDA build.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu \
        torch>=2.2 \
    && pip install --no-cache-dir .

RUN mkdir -p /data/mulch /data/workflow

EXPOSE 7777 7778

# v5.3.0: launch via the installed `prism` console-script so the docker
# entrypoint matches what a `pip install`'d user gets. Foreground (no
# --daemon) so docker controls process lifetime.
CMD ["prism", "start"]
