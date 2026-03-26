FROM node:20-alpine AS builder

WORKDIR /app
COPY package.json tsconfig.json ./
RUN npm install
COPY src/ src/
RUN npm run build

FROM node:20-alpine

RUN addgroup -g 1001 -S oracle && \
    adduser -u 1001 -S oracle -G oracle
WORKDIR /app
COPY --from=builder /app/dist dist/
COPY --from=builder /app/node_modules node_modules/
COPY --from=builder /app/package.json .
USER oracle

ENTRYPOINT ["node", "dist/index.js"]
