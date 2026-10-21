FROM python:3.11-slim

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY canary.py sql_guard.py ./

ENV PORT=8085
EXPOSE 8085

CMD ["python", "canary.py"]
