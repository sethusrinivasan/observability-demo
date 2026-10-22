# Multi-stage build for Swift Observability Microservice
FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 AS lambda-adapter

FROM swift:6.0 AS builder

WORKDIR /build
COPY Package.swift ./
COPY Sources ./Sources

# Build executable in release mode with optimizations
RUN swift build -c release --static-swift-stdlib

FROM swift:6.0-slim

# Install curl for health probes and diagnostic inspection
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*

# Copy AWS Lambda Web Adapter for serverless Graviton MicroVM execution
COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/version \
    AWS_LWA_PORT=8080

WORKDIR /app
COPY --from=builder /build/.build/release/observability-swift-app /app/observability-swift-app

EXPOSE 8080

CMD ["/app/observability-swift-app"]
