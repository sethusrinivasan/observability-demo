FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY receiver.py .

ENV PORT=4318
ENV BUFFER_DIR=/tmp/telemetry_buffer
ENV FLUSH_COUNT_THRESHOLD=1000
ENV FLUSH_TIME_THRESHOLD_SECONDS=300

EXPOSE 4318

CMD ["gunicorn", "--workers=1", "--threads=8", "--bind=0.0.0.0:4318", "--timeout=60", "receiver:app"]
