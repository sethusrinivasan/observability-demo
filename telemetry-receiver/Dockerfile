FROM python:3.12-slim

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY receiver.py .

ENV PORT=4318
ENV BUFFER_DIR=/tmp/telemetry_buffer
ENV FLUSH_COUNT_THRESHOLD=1000
ENV FLUSH_TIME_THRESHOLD_SECONDS=300

EXPOSE 4318

CMD ["python", "receiver.py"]
