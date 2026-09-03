FROM python:3.12-slim

WORKDIR /app

# Install Tesseract engine and regional language packs
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-hin \
    tesseract-ocr-ben \
    tesseract-ocr-mar \
    tesseract-ocr-tam \
    tesseract-ocr-guj \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files (including tessdata/ if present)
COPY . .

# Ensure data directory exists for persistent SQLite storage
RUN mkdir -p /data /app/tessdata

# Configure environment variables
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV PORT=10000
ENV DB_PATH=/data/land_records.db

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1"]