"""
pseudolabel.py
--------------
The final step of the semi-automated labelling pipeline.
Ghep cluster map + cluster->class mapping -> GeoTIFF label raster.

Input:
    data/clusters/<site>/<stem>.tif   (cluster maps per tile, int16)
    data/clusters/<site>/cluster_class_mapping.yaml

Output:
    data/pseudo/<site>/<stem>_label.tif   (class ID per pixel, uint8)

Usage:
    python src/pseudolabel.py --config config.yaml --site site_A
"""

import os
import argparse
from pathlib import Path

import numpy as np
import rasterio
import yaml
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_mapping(mapping_yaml: str) -> dict:
    """Returns dict: cluster_id (int) -> class_id (int)"""
    with open(mapping_yaml) as f:
        data = yaml.safe_load(f)
    return {int(k): int(v) for k, v in data["cluster_to_class_id"].items()}


def apply_mapping_to_tile(
    cluster_map: np.ndarray,
    mapping: dict,
    background_class: int = 0,
) -> np.ndarray:
    """
    cluster_map: (H, W) int16  -- cluster IDs
    mapping: dict cluster_id -> class_id
    Returns: label_map (H, W) uint8
    """
    label_map = np.full(cluster_map.shape, fill_value=background_class, dtype=np.uint8)

    for cid, class_id in mapping.items():
        label_map[cluster_map == cid] = class_id

    return label_map


def generate_pseudolabels(
    clusters_dir: str,
    mapping_yaml: str,
    out_dir: str,
    background_class: int = 0,
) -> list:
    """
    Create a pseudo-label GeoTIFF for every tile.
    Resume-safe: skips tiles that already have an output.
    """
    clusters_dir = Path(clusters_dir)
    out_dir      = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mapping = load_mapping(mapping_yaml)
    print(f"Loaded mapping: {len(mapping)} clusters")

    # Only process the cluster TIFs (they carry georeference), not the .npy files
    cluster_tiles = sorted(clusters_dir.glob("tile_*.tif"))
    if not cluster_tiles:
        raise ValueError(f"No tile_*.tif cluster maps found in {clusters_dir}")

    out_paths = []

    for ct in tqdm(cluster_tiles, desc="Pseudo-labeling"):
        out_path = out_dir / (ct.stem + "_label.tif")
        if out_path.exists():
            out_paths.append(str(out_path))
            continue  # resume-safe

        with rasterio.open(str(ct)) as src:
            cluster_map = src.read(1).astype(np.int16)  # (H, W)
            profile = src.profile.copy()

        label_map = apply_mapping_to_tile(
            cluster_map=cluster_map,
            mapping=mapping,
            background_class=background_class,
        )

        profile.update({
            "count": 1,
            "dtype": "uint8",
            "nodata": 255,  # 255 = unassigned/nodata
        })

        with rasterio.open(str(out_path), "w", **profile) as dst:
            dst.write(label_map[np.newaxis])

        out_paths.append(str(out_path))

    print(f"Generated {len(out_paths)} pseudo-label tiles -> {out_dir}")

    # Print class distribution
    print_class_distribution(out_paths)

    return out_paths


def print_class_distribution(label_paths: list) -> None:
    """Print the class distribution to check for imbalance."""
    from collections import Counter
    counter = Counter()

    for lp in label_paths:
        with rasterio.open(lp) as src:
            data = src.read(1).flatten()
        counter.update(data.tolist())

    total = sum(counter.values())
    print("\nClass distribution in pseudo-labels:")
    for class_id in sorted(counter.keys()):
        if class_id == 255:
            continue
        pct = counter[class_id] / total * 100
        print(f"  Class {class_id:2d}: {counter[class_id]:>10,} px  ({pct:.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="Generate pseudo-label GeoTIFFs from cluster maps")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--site", required=True)
    parser.add_argument("--mapping", default=None,
                        help="Path to cluster_class_mapping.yaml (default: auto from config + site)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    clusters_dir = os.path.join(cfg["paths"]["clusters_dir"], args.site)
    out_dir      = os.path.join(cfg["paths"]["pseudo_dir"], args.site)
    mapping_yaml = args.mapping or os.path.join(clusters_dir, "cluster_class_mapping.yaml")

    if not os.path.exists(mapping_yaml):
        raise FileNotFoundError(
            f"Mapping not found: {mapping_yaml}\n"
            f"Run first: python src/naming.py --config config.yaml --site {args.site} --apply-mapping"
        )

    generate_pseudolabels(
        clusters_dir=clusters_dir,
        mapping_yaml=mapping_yaml,
        out_dir=out_dir,
    )

    print(f"\nPseudo-labels ready in: {out_dir}")
    print(f"Next: python src/train.py --config config.yaml --site {args.site}")


if __name__ == "__main__":
    main()
