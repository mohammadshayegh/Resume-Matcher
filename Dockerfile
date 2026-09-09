# Resume Matcher Docker Image
# Multi-stage build for optimized image size

# ============================================
# Stage 1: Build Frontend
# ============================================
FROM node:22-bookworm AS frontend-builder

# Build argument for API URL (allows customization at build time)
# Default routes requests through Next.js rewrites on the same origin.
ARG NEXT_PUBLIC_API_URL=/

# Supabase (Google OAuth) must be supplied at BUILD time, not run time:
# Next.js inlines every NEXT_PUBLIC_* value into the client bundle during
# `npm run build`, so passing these only as container environment variables
# would leave the browser with no Supabase project and silently disable
# sign-in. Both are publishable by design (the anon key grants only what your
# Supabase policies grant), so baking them into the image is expected.
#
# Leave both empty to build a single-user image: no sign-in screen, all data
# owned by one implicit local account.
#
#   docker build \
#     --build-arg NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co \
#     --build-arg NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key .
#
# The backend's matching SUPABASE_URL is a normal runtime variable
# (see docker-compose.yml).
ARG NEXT_PUBLIC_SUPABASE_URL=
ARG NEXT_PUBLIC_SUPABASE_ANON_KEY=

ENV NEXT_TELEMETRY_DISABLED=1 \
    NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_SUPABASE_URL=${NEXT_PUBLIC_SUPABASE_URL} \
    NEXT_PUBLIC_SUPABASE_ANON_KEY=${NEXT_PUBLIC_SUPABASE_ANON_KEY}

WORKDIR /app/frontend

# Copy package files first for better caching
COPY apps/frontend/package*.json ./

# Install dependencies
RUN npm ci

# Copy frontend source
COPY apps/frontend/ ./

# Build the frontend
RUN npm run build

# ============================================
# Stage 2: Codex CLI (optional AI backend)
#
# Only needed when the backend runs with LLM_PROVIDER=codex. It is built in a
# node stage because the npm package resolves a platform-specific native binary
# through optionalDependencies at install time — so this must be built for the
# same platform/arch as the final image (it is: both are bookworm).
#
# The version is pinned: the CLI's model catalog and its `exec --json` event
# names are part of the contract app/codex_cli.py parses.
# ============================================
FROM node:22-bookworm AS codex-builder

ARG CODEX_VERSION=0.152.1
RUN npm install -g --prefix /codex @openai/codex@${CODEX_VERSION}

# ============================================
# Stage 3: Final Image
# ============================================
FROM python:3.13-slim-bookworm

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    # Playwright dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libgtk-3-0 \
    # CJK fonts for Chinese/Japanese/Korean PDF rendering via Playwright
    fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy Node.js runtime from frontend builder for reproducible runtime behavior.
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node

# ============================================
# Codex CLI
#
# Inert unless LLM_PROVIDER=codex, so it costs nothing but image size for
# other providers. Its launcher (bin/codex.js) runs on the node binary copied
# above.
#
# CODEX_HOME points at the persisted data volume: Codex keeps its credentials
# and its session rollouts there, and the backend reads remaining quota out of
# those rollouts. Putting it on the volume means `codex login` survives a
# container restart. Auth is deliberately NOT baked into the image — after
# first start, run:
#     docker compose exec <service> codex login
# (or mount an already-authenticated host directory at this path).
# ============================================
COPY --from=codex-builder /codex/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && chmod +x /usr/local/lib/node_modules/@openai/codex/bin/codex.js
ENV CODEX_HOME=/app/backend/data/.codex

# ============================================
# Backend Setup
# ============================================
COPY apps/backend/pyproject.toml /app/backend/
COPY apps/backend/app /app/backend/app

WORKDIR /app/backend

# Install Python dependencies
RUN pip install .

# ============================================
# Frontend Setup
# ============================================
WORKDIR /app/frontend

# Copy standalone frontend runtime from builder stage
COPY --from=frontend-builder /app/frontend/.next/standalone ./
COPY --from=frontend-builder /app/frontend/.next/static ./.next/static
COPY --from=frontend-builder /app/frontend/public ./public

# ============================================
# Startup Script
# ============================================
COPY docker/start.sh /app/start.sh
# Convert CRLF to LF (fixes Windows line ending issues) and make executable
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# ============================================
# Data Directory & Volume
# ============================================
RUN mkdir -p /app/backend/data "$CODEX_HOME"

# Create a non-root user for security
RUN useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

# Install Playwright Chromium as appuser (so browsers are in correct location)
RUN python -m playwright install chromium

# Expose the public port (backend remains internal on 8000)
EXPOSE 3000

# Volume for persistent data
VOLUME ["/app/backend/data"]

# Set working directory
WORKDIR /app

# Health check on internal backend port only (independent of host port mapping).
HEALTHCHECK --interval=10s --timeout=10s --start-period=30s --retries=5 \
    CMD curl -f http://127.0.0.1:8000/api/v1/health || exit 1

# Start the application
CMD ["/app/start.sh"]
