#!/usr/bin/env bash
# Builds $DATA_DIR/raw/lola_global/ldem_256.tif (118 m/px, whole Moon) from the
# four LOLA GDR LDEM_256 tiles, so the engine samples it instead of the 474 m
# ldem_64.tif off the south pole polar DEM.
#
#   scripts/build_global_dem.sh <dir with LDEM_256_*.IMG + .LBL> <output .tif>
#
# Source (PDS, ~8.5 GB): https://imbrium.mit.edu/DATA/LOLA_GDR/CYLINDRICAL/IMG/
#   LDEM_256_00N_90N_000_180  LDEM_256_00N_90N_180_360
#   LDEM_256_90S_00S_000_180  LDEM_256_90S_00S_180_360   (.IMG and .LBL each)
#
# Output: geographic (+proj=longlat, R = 1737.4 km), longitudes 0..360, int16
# DN with scale 0.5 and offset 0, so height (m) = DN * 0.5 relative to
# 1737.4 km. GDAL reads the PDS labels with offset 1737400 (planetary radius):
# it is forced to 0 here because the engine expects heights.
# Runs GDAL from its Docker image; nothing to install on the host.
set -euo pipefail

SRC=$(cd "${1:?tiles directory}" && pwd)
OUT=${2:?output .tif}
OUT_DIR=$(mkdir -p "$(dirname "$OUT")" && cd "$(dirname "$OUT")" && pwd)
NAME=$(basename "$OUT")
GDAL_IMAGE=${GDAL_IMAGE:-ghcr.io/osgeo/gdal:alpine-small-3.10.0}

for t in 00N_90N_000_180 00N_90N_180_360 90S_00S_000_180 90S_00S_180_360; do
  [[ -s "$SRC/LDEM_256_$t.IMG" && -s "$SRC/LDEM_256_$t.LBL" ]] || {
    echo "missing LDEM_256_$t.IMG/.LBL in $SRC" >&2
    exit 1
  }
done

gdal() { docker run --rm -u "$(id -u):$(id -g)" -v "$SRC:/in:ro" -v "$OUT_DIR:/out" "$GDAL_IMAGE" "$@"; }

# The four tiles share one projected CRS (center longitude 180), so a VRT
# mosaics them exactly; its pixel grid is 256 px/deg over 0..360 x -90..90.
gdal gdalbuildvrt -q "/out/.$NAME.vrt" /in/LDEM_256_00N_90N_000_180.LBL /in/LDEM_256_00N_90N_180_360.LBL \
  /in/LDEM_256_90S_00S_000_180.LBL /in/LDEM_256_90S_00S_180_360.LBL
gdal gdal_translate -q \
  -a_srs "+proj=longlat +R=1737400 +no_defs +type=crs" -a_ullr 0 90 360 -90 \
  -a_scale 0.5 -a_offset 0 \
  -co TILED=YES -co BLOCKXSIZE=512 -co BLOCKYSIZE=512 \
  -co COMPRESS=DEFLATE -co PREDICTOR=2 -co BIGTIFF=YES -co NUM_THREADS=ALL_CPUS \
  "/out/.$NAME.vrt" "/out/.$NAME.tmp"
rm -f "$OUT_DIR/.$NAME.vrt"
# The engine ignores *.tmp files, so the DEM only appears once it is complete.
mv "$OUT_DIR/.$NAME.tmp" "$OUT_DIR/$NAME"
gdal gdalinfo "/out/$NAME" | grep -E "Size is|Origin|Pixel Size|Offset|Scale"
