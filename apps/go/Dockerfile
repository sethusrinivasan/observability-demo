# Build stage (runs natively on build host and cross-compiles to target architecture)
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS builder
ARG TARGETOS
ARG TARGETARCH
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=${TARGETOS:-linux} GOARCH=${TARGETARCH:-arm64} go build -ldflags="-w -s" -o /app/server .

# Runtime stage
FROM alpine:3.20
RUN apk --no-cache add ca-certificates wget
WORKDIR /app
COPY --from=builder /app/server /app/server

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/app/server"]
