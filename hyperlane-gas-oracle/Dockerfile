FROM node:20-alpine AS builder

WORKDIR /app
COPY package.json ./
RUN npm install --production
COPY src/ src/

FROM node:20-alpine

RUN addgroup -g 1001 -S oracle && \
    adduser -u 1001 -S oracle -G oracle
WORKDIR /app
COPY --from=builder /app /app
USER oracle

ENTRYPOINT ["node", "src/index.js"]
