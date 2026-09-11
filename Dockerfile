FROM python:3.12-slim

WORKDIR /app

# Open-source OCR engine and language packs used by the existing application.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-hin \
    tesseract-ocr-tel \
    tesseract-ocr-tam \
    tesseract-ocr-ben \
    tesseract-ocr-mar \
    tesseract-ocr-guj \
    tesseract-ocr-pan \
    tesseract-ocr-kan \
    tesseract-ocr-ori \
    tesseract-ocr-urd \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

ENV APP_ENV=production
ENV OMP_THREAD_LIMIT=1
ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV PORT=10000
ENV MAP_TILE_URL=https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png
ENV MAP_ATTRIBUTION="© OpenStreetMap contributors"
ENV LAND_AREA_TOLERANCE_HA=0.05

EXPOSE 10000

USER appuser

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
