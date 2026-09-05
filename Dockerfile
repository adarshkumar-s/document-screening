FROM python:3.12-slim

WORKDIR /app

# Install Tesseract engine and regional language packs including Telugu
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-hin \
    tesseract-ocr-tel \
    tesseract-ocr-tam \
    tesseract-ocr-ben \
    tesseract-ocr-mar \
    tesseract-ocr-guj \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

# Prevent CPU thread contention on single-core cloud containers
ENV OMP_THREAD_LIMIT=1
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV PORT=10000

EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1"]