"""
cluster.py
----------
K-means over-clustering on DINOv2 features.

Strategy: deliberate over-clustering (30-50 clusters).
- Sphagnum R/G/Y separate on their own because their colours differ -> 3 clusters
- Dracophyllum may split into 2 seasonal clusters (green / red)
- E. minus and P. costiniana may end up merged (multispectral is needed to split them)
- A human only names ~40 clusters instead of tracing every polygon

Usage:
    python src/cluster.py --config config.yaml --site site_A
    # Reads:  data/features/site_A/*.npy
    # Writes: data/clusters/site_A/cluster_map.npy  (int16, pixel -> cluster_id)
              data/clusters/site_A/cluster_map.tif  (GeoTIFF, same CRS as ortho)
              data/clusters/site_A/centroids.npy    (n_clusters x C, float32)
"""

import os
import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds
import yaml
from sklearn.cluster import MiniBatchKMeans
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_all_features(features_dir: str, tile_size: int = 512) -> tuple:
    """
    Read every .npy feature file in the folder.
    Returns:
        all_features: (N_pixels, C) float32  -- all pixel features
        tile_info: list of (stem, H, W)      -- de rebuild cluster map
    """
    feat_paths = sorted(Path(features_dir).glob("*.npy"))
    if not feat_paths:
        raise ValueError(f"No .npy files in {features_dir}")

    all_features = []
    tile_info = []

    for fp in tqdm(feat_paths, desc="Loading features"):
        feat = np.load(str(fp))  # (H, W, C)
        H, W, C = feat.shape
        tile_info.append((fp.stem, H, W))
        all_features.append(feat.reshape(-1, C))  # (H*W, C)

    all_features = np.concatenate(all_features, axis=0)  # (Total_pixels, C)
    return all_features, tile_info, C


def fit_kmeans(
    features: np.ndarray,
    n_clusters: int,
    sample_fraction: float = 0.05,
    random_state: int = 42,
    max_iter: int = 500,
    n_init: int = 10,
) -> MiniBatchKMeans:
    """
    Fit MiniBatchKMeans on a fraction of the pixels (memory-safe).
    MiniBatchKMeans is 5-10x faster than KMeans on large arrays.
    """
    N = features.shape[0]
    n_sample = max(int(N * sample_fraction), n_clusters * 100)
    n_sample = min(n_sample, N)

    print(f"Fitting k-means: {n_clusters} clusters on {n_sample:,} / {N:,} pixels")

    rng = np.random.default_rng(random_state)
    idx = rng.choice(N, size=n_sample, replace=False)
    sample = features[idx]

    kmeans = MiniBatchKMeans(
        n_clusters=n_clusters,
        random_state=random_state,
        max_iter=max_iter,
        n_init=n_init,
        batch_size=min(4096, n_sample),
        verbose=0,
    )
    kmeans.fit(sample)
    print("K-means fit done.")
    return kmeans


def assign_clusters(
    features: np.ndarray,
    kmeans: MiniBatchKMeans,
    batch_size: int = 500_000,
) -> np.ndarray:
    """
    Assign cluster labels to every pixel, batch by batch (memory-safe).
    Returns: labels (N,) int16
    """
    N = features.shape[0]
    labels = np.empty(N, dtype=np.int16)

    for start in tqdm(range(0, N, batch_size), desc="Assigning clusters"):
        end = min(start + batch_size, N)
        labels[start:end] = kmeans.predict(features[start:end]).astype(np.int16)

    return labels


def rebuild_cluster_maps(
    labels: np.ndarray,
    tile_info: list,
    features_dir: str,
    tiles_dir: str,
    out_dir: str,
) -> dict:
    """
    Split the 1D labels back into a cluster map per tile.
    Write each tile as .npy and .tif (CRS copied from the matching image tile).

    Returns: dict stem -> cluster_map array (H, W)
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tile_maps = {}
    offset = 0

    for stem, h_p, w_p in tqdm(tile_info, desc="Rebuilding maps"):
        n_px = h_p * w_p
        tile_labels_small = labels[offset:offset + n_px].reshape(h_p, w_p)
        offset += n_px

        # Features are stored at patch resolution (h_p x w_p) -> upsample the
        # cluster map (nearest-neighbour, int16) back to the original tile size.
        img_tile_path = Path(tiles_dir) / f"{stem}.tif"
        profile = None
        if img_tile_path.exists():
            with rasterio.open(str(img_tile_path)) as src:
                H_true, W_true = src.height, src.width
                profile = src.profile.copy()
        else:
            H_true, W_true = h_p, w_p

        ys = np.minimum((np.arange(H_true) * h_p) // H_true, h_p - 1)
        xs = np.minimum((np.arange(W_true) * w_p) // W_true, w_p - 1)
        tile_labels = tile_labels_small[np.ix_(ys, xs)].astype(np.int16)

        # Save .npy
        npy_path = out_dir / f"{stem}.npy"
        np.save(str(npy_path), tile_labels)

        # Save .tif (transform copied from the image tile)
        if profile is not None:
            # Drop the options that are only valid for RGB images
            for k in ("photometric", "compress", "interleave"):
                profile.pop(k, None)
            profile.update({
                "count": 1,
                "dtype": "int16",
                "nodata": -1,
                "compress": "deflate",
            })
            tif_path = out_dir / f"{stem}.tif"
            with rasterio.open(str(tif_path), "w", **profile) as dst:
                dst.write(tile_labels[np.newaxis])

        tile_maps[stem] = tile_labels

    return tile_maps


def main():
    parser = argparse.ArgumentParser(description="K-means clustering on DINOv2 features")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--site", required=True, help="Site name")
    args = parser.parse_args()

    cfg = load_config(args.config)
    c = cfg["clustering"]

    features_dir = os.path.join(cfg["paths"]["features_dir"], args.site)
    tiles_dir    = os.path.join(cfg["paths"]["tiles_dir"], args.site, "images")
    out_dir      = os.path.join(cfg["paths"]["clusters_dir"], args.site)

    # 1. Load all features
    all_features, tile_info, C = load_all_features(features_dir)
    print(f"Total pixels: {all_features.shape[0]:,}  | Feature dim: {C}")

    # 2. Fit k-means
    kmeans = fit_kmeans(
        all_features,
        n_clusters=c["n_clusters"],
        sample_fraction=c["sample_fraction"],
        random_state=c["random_state"],
        max_iter=c["max_iter"],
        n_init=c["n_init"],
    )

    # 3. Assign all pixels
    labels = assign_clusters(all_features, kmeans)

    # 4. Save centroids
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    np.save(str(out_path / "centroids.npy"), kmeans.cluster_centers_.astype(np.float32))
    print(f"Centroids saved: {out_path / 'centroids.npy'}")

    # 5. Rebuild per-tile cluster maps
    rebuild_cluster_maps(
        labels=labels,
        tile_info=tile_info,
        features_dir=features_dir,
        tiles_dir=tiles_dir,
        out_dir=out_dir,
    )

    print(f"\nClustering done. {c['n_clusters']} clusters -> {out_dir}")
    print("Next step: python src/naming.py --config config.yaml --site", args.site)


if __name__ == "__main__":
    main()
