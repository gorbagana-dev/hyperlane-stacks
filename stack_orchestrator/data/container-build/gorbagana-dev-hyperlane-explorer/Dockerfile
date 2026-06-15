# Stage 1: Build Next.js app
#
# Build context: ~/cerc/hyperlane-explorer (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-explorer
# Note: build.sh copies entrypoint.sh into the build context before building.

FROM node:24-alpine AS builder

RUN corepack enable && corepack prepare pnpm@11.1.3 --activate

WORKDIR /app

# Copy only dependency files first — pnpm install is cached until these change.
# Include patches/ since pnpm-lock.yaml references patchedDependencies.
COPY package.json pnpm-lock.yaml ./
COPY patches/ patches/

# Install dependencies (cached layer — only reruns when package.json/lockfile change)
RUN pnpm install --frozen-lockfile || pnpm install

# Now copy the rest of the source
COPY . .

# Enable standalone output mode for a minimal runtime image (~200MB vs ~4GB).
# Produces .next/standalone/ with only required node_modules and server.js.
RUN sed -i 's/reactStrictMode: true,/reactStrictMode: true,\n  output: "standalone",/' next.config.js

RUN pnpm build

# Stage 2: Runtime (standalone — no pnpm or full node_modules needed)
FROM node:24-alpine

RUN addgroup -g 1001 -S explorer && \
    adduser -u 1001 -S explorer -G explorer

WORKDIR /app

# Copy standalone server (includes minimal node_modules + server.js)
COPY --from=builder --chown=explorer:explorer /app/.next/standalone ./

# Copy static assets and public dir into the standalone tree
COPY --from=builder --chown=explorer:explorer /app/.next/static .next/static
COPY --from=builder --chown=explorer:explorer /app/public public

# Copy entrypoint (placed into build context by build.sh)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER explorer

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
