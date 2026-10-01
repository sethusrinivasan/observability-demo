# Build stage
FROM alpine:3.20 AS builder

RUN apk --no-cache add \
    gcc \
    musl-dev \
    make \
    pkgconf \
    libpq-dev \
    curl-dev

WORKDIR /app
COPY . .
RUN make clean && make

# Runtime stage
FROM alpine:3.20
RUN apk --no-cache add \
    ca-certificates \
    libpq \
    libcurl \
    wget \
    curl

WORKDIR /app
COPY --from=builder /app/server /app/server

EXPOSE 8080
ENTRYPOINT ["/app/server"]
