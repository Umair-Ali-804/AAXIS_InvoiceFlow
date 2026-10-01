# AAXIS InvoiceFlow: one container (pages + API) for Render, Hugging Face, Koyeb or any Docker host.
# Listens on $PORT (Render: 10000 via render.yaml; default 7860). Secrets come from the host's dashboard.
FROM python:3.12-slim-bookworm

# Tesseract for OCR of scans and photos; libglib for OpenCV.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    DATA_DIR=/home/user/app/data
WORKDIR /home/user/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
RUN mkdir -p data && chown -R user:user /home/user/app
USER user
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/healthz')"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips='*'"]
