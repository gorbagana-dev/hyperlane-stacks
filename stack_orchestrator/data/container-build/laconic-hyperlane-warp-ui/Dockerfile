# Stage 1: Build Next.js app with sentinel placeholders
#
# Build context: ~/cerc/hyperlane-warp-ui-template (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-warp-ui-template
# Note: build.sh copies entrypoint.sh and configs/ into the build context before building.

FROM node:20-alpine AS builder

RUN corepack enable && corepack prepare pnpm@9 --activate

WORKDIR /app

# Copy warp UI template from build context (cloned to ~/cerc/ by laconic-so)
COPY . .

# Create placeholder chain config with sentinel values
COPY configs/chains.yaml src/consts/chains.yaml
COPY configs/warpRoutes.yaml src/consts/warpRoutes.yaml

# Create .env with sentinel NEXT_PUBLIC_ values
COPY configs/.env.sentinel .env

# Install dependencies
RUN pnpm install --frozen-lockfile || pnpm install

# Build with sentinels baked in
RUN pnpm build

# Stage 2: Runtime
FROM node:20-alpine

RUN corepack enable && corepack prepare pnpm@9 --activate

RUN addgroup -g 1001 -S warpui && \
    adduser -u 1001 -S warpui -G warpui

WORKDIR /app

# Copy built app
COPY --from=builder /app/.next .next
COPY --from=builder /app/public public
COPY --from=builder /app/package.json package.json
COPY --from=builder /app/node_modules node_modules
COPY --from=builder /app/next.config.js next.config.js 2>/dev/null || true
COPY --from=builder /app/next.config.mjs next.config.mjs 2>/dev/null || true

# Copy entrypoint (placed into build context by build.sh)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Make .next writable for sed replacements
RUN chown -R warpui:warpui /app

USER warpui

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
