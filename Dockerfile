# syntax=docker/dockerfile:1
# Auspex backend/domain container (arc42 §7 "Deployment View").
#
# This single image serves all three deployment targets described in arc42 §7;
# each Container Apps resource pins its own command in infra/modules/containerapps.bicep:
#   - app-auspex-api          (default CMD below: `auspex serve`, port 8080)
#   - job-auspex-pipeline     (command override: `python -m auspex nightly`)
#   - job-auspex-performance  (command override: `python -m auspex performance`)
# `python -m auspex bootstrap` runs the one-time cold start (arc42 §6.3) the same way.
#
# The `app-auspex-api` target also serves the compiled React 18/Vite SPA
# (arc42 §7 deployment view): the `web-builder` stage below builds `web/` into
# `web/dist`, which is copied into the runtime stage and served by
# auspex.api.static alongside `/api/*` and `/healthz` — one production
# container, no separate static host.
#
# No secrets or connection strings are baked into the image (arc42 TC-04):
# Cosmos/Blob/Azure OpenAI access is via system-assigned managed identity at
# runtime, and third-party API keys are resolved from Key Vault by
# auspex.providers.secrets.SecretResolver.

FROM node:22-alpine AS web-builder

WORKDIR /web

# Copy only the manifests first so `npm ci` is cached independently of source
# changes; the SPA's own node_modules/dist are never copied from the host.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/index.html web/vite.config.ts web/tsconfig.json web/tsconfig.app.json web/tsconfig.node.json \
     web/postcss.config.js web/tailwind.config.js ./
COPY web/public ./public
COPY web/src ./src

RUN npm run build

FROM python:3.12-slim AS builder

WORKDIR /build
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --prefix=/install .

FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 auspex
WORKDIR /app

COPY --from=builder /install /usr/local
COPY config ./config
COPY prompts ./prompts
COPY --from=web-builder /web/dist ./web/dist

ENV AUSPEX_CONFIG_DIR=/app/config \
    AUSPEX_PROMPTS_DIR=/app/prompts \
    AUSPEX_WEB_DIST_DIR=/app/web/dist \
    PYTHONUNBUFFERED=1

USER auspex

# Matches the Container Apps ingress `targetPort` and the `/healthz` liveness/
# readiness probes in infra/modules/containerapps.bicep.
EXPOSE 8080

ENTRYPOINT ["auspex"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
