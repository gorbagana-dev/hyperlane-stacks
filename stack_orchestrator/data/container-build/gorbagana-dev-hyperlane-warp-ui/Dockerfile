# Stage 1: Build Next.js app
#
# Build context: ~/cerc/hyperlane-warp-ui-template (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-warp-ui-template
# Note: build.sh copies entrypoint.sh into the build context before building.

FROM node:24-alpine AS builder

RUN corepack enable && corepack prepare pnpm@9 --activate

WORKDIR /app

# Copy only dependency files first — pnpm install is cached until these change
# Include patches/ dir since pnpm-lock.yaml references patchedDependencies
COPY package.json pnpm-lock.yaml ./
COPY patches/ patches/

# Install dependencies (cached layer — only reruns when package.json/lockfile change)
RUN pnpm install --frozen-lockfile || pnpm install

# Now copy the rest of the source
COPY . .

# Enable standalone output mode for minimal runtime image (~200MB vs ~4GB).
# Produces .next/standalone/ with only required node_modules and server.js.
# Also remove outputFileTracingExcludes — those exclude @solana, @cosmjs etc.
# from file tracing, which is fine for Vercel serverless but breaks standalone
# mode where the server needs these modules at runtime.
RUN sed -i 's/reactStrictMode: true,/reactStrictMode: true,\n  output: "standalone",/' next.config.js && \
    sed -i '/outputFileTracingExcludes/,/^  },/d' next.config.js

# NEXT_PUBLIC_* is baked into the JS bundle at build time, so the WalletConnect ID
# must be passed as a build arg (not a runtime env). With laconic-so, set:
#   CERC_CONTAINER_EXTRA_BUILD_ARGS='--build-arg NEXT_PUBLIC_WALLET_CONNECT_ID=<id>'
# Empty is fine for e2e/local; the published prod image must pass the real id.
ARG NEXT_PUBLIC_WALLET_CONNECT_ID=""
ENV NEXT_PUBLIC_WALLET_CONNECT_ID=${NEXT_PUBLIC_WALLET_CONNECT_ID}

RUN pnpm build

# Stage 2: Runtime (standalone — no pnpm or full node_modules needed)
FROM node:24-alpine

RUN addgroup -g 1001 -S warpui && \
    adduser -u 1001 -S warpui -G warpui

WORKDIR /app

# Copy standalone server (includes minimal node_modules + server.js)
COPY --from=builder --chown=warpui:warpui /app/.next/standalone ./

# Copy static assets and public dir into the standalone tree
COPY --from=builder --chown=warpui:warpui /app/.next/static .next/static
COPY --from=builder --chown=warpui:warpui /app/public public

# Copy entrypoint (placed into build context by build.sh)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER warpui

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
