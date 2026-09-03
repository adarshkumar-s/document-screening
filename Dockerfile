FROM python:3.12-slim

WORKDIR /app

# Install Tesseract, English, Hindi, Bengali, Marathi, and Tamil traineddata
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-hin \
    tesseract-ocr-ben \
    tesseract-ocr-mar \
    tesseract-ocr-tam \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure data directory exists for persistent SQLite storage
RUN mkdir -p /data

ENV PORT=10000
ENV DB_PATH=/data/land_records.db
EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1"]