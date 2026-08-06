#!/usr/bin/env bash
# Upload v2 parquet files to R2
set -euo pipefail

V2_DIR="/home/aoi/kino/projects/ptiles/data/parquet/v2"
PROFILE="mdt-r2"
BUCKET="s3://mydatatimeline/earth/v2"

echo "=== Uploading v2 files to R2 ==="

# Upload per-state directories
for state_dir in "$V2_DIR"/*/; do
  state=$(basename "$state_dir")
  echo "  $state..."
  AWS_PROFILE="$PROFILE" aws s3 cp "$state_dir" "$BUCKET/$state/" \
    --recursive --exclude "*" --include "*_v2.parquet" \
    --quiet
done

# Upload manifest
echo "  manifest..."
AWS_PROFILE="$PROFILE" aws s3 cp "$V2_DIR/manifest.json" "$BUCKET/manifest.json" \
  --content-type "application/json" --quiet

# Also copy business_v1.parquet to v2 dir for search fallback
BUSINESS_V1="/home/aoi/kino/projects/ptiles/data/parquet/business_v1.parquet"
if [ -f "$BUSINESS_V1" ]; then
  echo "  business_v1.parquet (search fallback)..."
  AWS_PROFILE="$PROFILE" aws s3 cp "$BUSINESS_V1" "$BUCKET/business/business_v1.parquet" --quiet
fi

echo "=== Done ==="
