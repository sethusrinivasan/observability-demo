# Build stage
FROM rust:latest AS builder
WORKDIR /app
COPY . .
RUN cargo build --release

# Run stage
FROM debian:bookworm-slim
WORKDIR /app
RUN apt-get update && apt-get install -y libssl3 ca-certificates curl wget && rm -rf /var/lib/apt/lists/*
COPY --from=builder /app/target/release/observability-rust-app .

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8081
EXPOSE 8081

CMD ["./observability-rust-app"]
