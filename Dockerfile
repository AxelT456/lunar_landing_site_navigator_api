FROM python:3.12-slim

# GDAL_CACHEMAX (MB): GDAL's block cache defaults to 5% of the host RAM. A container
# serving terrain tiles grew from ~150 MiB to ~1 GiB; 256 MB caps that cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GDAL_CACHEMAX=256

WORKDIR /app

# libexpat1: runtime dependency of the rasterio (GDAL) wheel on slim images.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefer-binary -r /app/requirements.txt

COPY app /app/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
