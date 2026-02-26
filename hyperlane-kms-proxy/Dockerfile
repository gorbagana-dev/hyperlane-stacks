FROM golang:1.24-alpine AS builder

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /kms-proxy .

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /kms-proxy /usr/local/bin/kms-proxy
USER nobody
EXPOSE 9999
ENTRYPOINT ["kms-proxy"]
