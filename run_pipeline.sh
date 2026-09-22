#!/usr/bin/env bash
# ============================================================
# Run the whole batch pipeline from start to finish
# Usage: bash run_pipeline.sh site_A data/raw/site_A.tif
# ============================================================
set -e

SITE=${1:-"site_A"}
ORTHO=${2:-"data/raw/site_A.tif"}
CONFIG="config.yaml"

echo "========================================"
echo " Bunjilview Alpine Bog Segmentation"
echo " Site: $SITE"
echo " Ortho: $ORTHO"
echo "========================================"

# Step 1: tile the orthomosaic
echo ""
echo "[1/6] Tiling orthomosaic..."
python src/tiling.py --config $CONFIG --ortho $ORTHO

# Step 2: extract features with DINOv2
echo ""
echo "[2/6] Extracting DINOv2 features (this can take 1-2 hours)..."
python src/features.py --config $CONFIG --site $SITE

# Step 3: K-means clustering
echo ""
echo "[3/6] Clustering (40 clusters)..."
python src/cluster.py --config $CONFIG --site $SITE

# Step 4a: export the montage for human naming
echo ""
echo "[4/6] Exporting cluster montage for human naming..."
python src/naming.py --config $CONFIG --site $SITE --export-montage

echo ""
echo ">>> STOP HERE: open this file and fill the 'species' column:"
echo "    data/clusters/$SITE/cluster_names.csv"
echo ">>> Look at the montage image here:"
echo "    data/clusters/$SITE/cluster_montage.png"
echo ">>> When you are done, continue with:"
echo "    bash run_pipeline.sh $SITE $ORTHO --from-naming"
echo ""

if [ "${3}" != "--from-naming" ]; then
    exit 0
fi

# Step 4b: apply the naming
echo ""
echo "[4b/6] Applying cluster naming..."
python src/naming.py --config $CONFIG --site $SITE --apply-mapping

# Step 5: generate the pseudo-labels
echo ""
echo "[5/6] Generating pseudo-label GeoTIFFs..."
python src/pseudolabel.py --config $CONFIG --site $SITE

echo ""
echo "========================================"
echo " Pseudo-labels ready in data/pseudo/$SITE/"
echo " Next step: train the model"
echo "   python src/train.py --config $CONFIG --site $SITE"
echo "========================================"
