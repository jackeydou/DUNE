# syntax=docker/dockerfile:1
# The Go binaries: edge, sandboxd, swarm-certs, and the swarm CLI. Build from the repo root:
#   docker build -f deploy/images/go.Dockerfile -t swarmeval/go .

FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go/go.mod go/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY go/ ./
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -o /out/ ./cmd/edge ./cmd/sandboxd ./cmd/swarm ./cmd/swarm-certs

FROM alpine:3.22
# The same uid as the Python image. /certs is owned by it so a new certificate volume is too.
RUN adduser -D -H -u 10001 swarmeval && mkdir /certs /config && chown 10001 /certs /config
COPY --from=build /out/ /usr/local/bin/
USER 10001
