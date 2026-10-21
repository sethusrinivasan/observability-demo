FROM python:3.11-slim

# Include AWS Lambda Web Adapter for serverless microVM execution
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4 /lambda-adapter /opt/extensions/lambda-adapter

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application modules — evaluator.py must be present alongside app.py
# so the /eval endpoint can import it at runtime
COPY app.py evaluator.py ./

ENV PORT=5000
EXPOSE 5000

CMD ["gunicorn", "--workers=1", "--threads=4", "--bind=0.0.0.0:5000", "--timeout=30", "app:app"]