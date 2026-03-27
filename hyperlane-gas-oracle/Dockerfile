FROM node:24-alpine AS builder

WORKDIR /app
COPY package.json yarn.lock ./
RUN yarn install --frozen-lockfile
COPY src/ src/
COPY tsconfig.json ./
RUN yarn build

FROM node:24-alpine

RUN addgroup -g 1001 -S oracle && \
    adduser -u 1001 -S oracle -G oracle
WORKDIR /app
COPY --from=builder /app/dist dist/
COPY --from=builder /app/node_modules node_modules/
COPY --from=builder /app/package.json .
USER oracle

ENTRYPOINT ["node", "dist/index.js"]
