FROM python:3.11-slim

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install DuckDB extensions for instant offline loading in Lambda
RUN mkdir -p /app/duckdb_extensions && \
    python -c "import duckdb; con = duckdb.connect(':memory:'); con.execute(\"SET home_directory='/tmp'; SET extension_directory='/app/duckdb_extensions'; INSTALL httpfs;\")"

COPY *.py ./

ENV PORT=8085
EXPOSE 8085

CMD ["python", "canary.py"]
