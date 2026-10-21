# Build stage
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /app
COPY pom.xml .
RUN mvn dependency:go-offline
COPY src ./src
RUN mvn package -DskipTests

# Run stage - Amazon Corretto 17 Alpine has native AWS Graviton ARM64 support
FROM amazoncorretto:17-alpine
RUN apk add --no-cache wget curl
WORKDIR /app
COPY --from=build /app/target/observability-java-app-0.0.1-SNAPSHOT.jar app.jar

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["java", "-XX:+TieredCompilation", "-XX:TieredStopAtLevel=1", "-Xmx192m", "-Xss512k", "-XX:+UseSerialGC", "-jar", "app.jar"]
