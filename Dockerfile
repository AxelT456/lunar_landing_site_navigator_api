FROM python:3.12-slim

# GDAL_CACHEMAX (MB): GDAL's block cache defaults to 5% of the host RAM. A container
# serving terrain tiles grew from ~150 MiB to ~1 GiB; 256 MB caps that cache.
# MALLOC_*: tile rendering allocates and frees many ~0.5-4 MB numpy arrays. glibc's defaults
# return them to the kernel with mmap/munmap every time; keeping them in the heap made a
# globe session 15-22% faster with a third of the peak memory (measured on the server).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GDAL_CACHEMAX=256 \
    MALLOC_MMAP_THRESHOLD_=33554432 \
    MALLOC_TRIM_THRESHOLD_=1073741824 \
    MALLOC_TOP_PAD_=268435456

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
