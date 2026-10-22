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

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/server"]
