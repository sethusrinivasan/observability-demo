FROM public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 AS lambda-adapter

FROM php:8.3-cli-alpine

# Upgrade musl/system packages to prevent version conflicts on arm64; install libpq, curl & pdo_pgsql
RUN apk update && apk upgrade \
    && apk add --no-cache libpq curl \
    && apk add --no-cache --virtual .build-deps postgresql-dev \
    && docker-php-ext-install pdo_pgsql \
    && apk del .build-deps \
    && rm -rf /var/cache/apk/*

# Copy AWS Lambda Web Adapter for Graviton MicroVM execution
COPY --from=lambda-adapter /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/version \
    AWS_LWA_PORT=8080 \
    PHP_CLI_SERVER_WORKERS=4

WORKDIR /app

COPY . /app

EXPOSE 8080

CMD ["php", "-S", "0.0.0.0:8080", "-t", "/app", "/app/index.php"]
