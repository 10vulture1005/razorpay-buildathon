# syntax=docker/dockerfile:1.6
# Build:    docker build -t recovery-dashboard frontend-next/
# Run:      docker run -p 3000:3000 -e API_URL=https://api.example.com -e API_KEY=<key> recovery-dashboard
#
# Required env at RUNTIME (NOT build time — but we read API_URL at build too,
# see below):
#   API_URL                  the public URL of the backend API
#   API_KEY                  a read-scope key (server-side only)
#   NEXT_PUBLIC_API_BASE_URL same as API_URL but exposed to the browser for
#                            direct (non-proxied) client calls in the
#                            merchant sandbox.
#
# Required env at BUILD time:
#   API_URL (or NEXT_PUBLIC_API_BASE_URL) must be set so the rewrites are
#   baked into the build. next.config.ts fail-fasts if missing in production.

FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Bake the API URL into the static rewrite map. Without this, the build
# defaults to localhost:8000 (only correct in dev).
ARG API_URL
ENV API_URL=${API_URL} \
    NEXT_PUBLIC_API_BASE_URL=${API_URL} \
    NEXT_TELEMETRY_DISABLED=1
# next.config.ts fail-fasts if API_URL is empty in production.
RUN test -n "$API_URL" || (echo "API_URL build arg is required" && exit 1)

RUN npm run build

# ---- runtime stage: standalone server (small, self-contained) ----
FROM node:20-alpine AS runner
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Standalone output puts the server in /app/server.js and bundles only the
# node_modules it actually needs in /app/.next/standalone. Copy the public
# + static assets separately.
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public 2>/dev/null || true

# Non-root runtime user.
RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs \
    && mkdir -p .next && chown -R nextjs:nodejs .next
USER nextjs

EXPOSE 3000

# Liveness: hit the home page; cheap and surfaces a 200 even before SSR.
HEALTHCHECK --interval=30s --timeout=5s --start_period=10s --retries=3 \
    CMD wget -qO- http://localhost:3000/ >/dev/null || exit 1

CMD ["node", "server.js"]
