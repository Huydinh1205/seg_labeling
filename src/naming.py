"""
naming.py
---------
The human-in-the-loop step: ~40 decisions instead of thousands of polygons.

1. Export a montage: a grid of representative patches for each cluster
   -> Human nhin montage, viet CSV: cluster_id, species_name
2. Apply the mapping: read the CSV, write cluster_name_map.yaml

Usage:
    # Step 1: export the montage for a human to look at
    python src/naming.py --config config.yaml --site site_A --export-montage

    # Step 2: once cluster_names.csv has been filled in
    python src/naming.py --config config.yaml --site site_A --apply-mapping \
        --csv data/clusters/site_A/cluster_names.csv

Format CSV:
    cluster_id,species
    0,Sphagnum_R
    1,Empidisma_minus
    2,None
    ...

Species names must match the keys in config.yaml -> classes.
"""

import os
import csv
import argparse
from pathlib import Path
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import rasterio
import yaml
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ============================================================
# STEP 1: Export the montage
# ============================================================

def collect_cluster_patches(
    tiles_dir: str,
    clusters_dir: str,
    n_clusters: int,
    patches_per_cluster: int = 12,
    patch_size: int = 64,
    seed: int = 42,
) -> dict:
    """
    For each cluster, sample `patches_per_cluster` random image patches.
    Returns: dict cluster_id -> list of PIL.Image
    """
    rng = np.random.default_rng(seed)
    cluster_patches = defaultdict(list)
    cluster_counts  = defaultdict(int)

    tile_paths = sorted(Path(tiles_dir).glob("*.tif"))

    for tp in tqdm(tile_paths, desc="Collecting patches"):
        cluster_path = Path(clusters_dir) / (tp.stem + ".npy")
        if not cluster_path.exists():
            continue

        with rasterio.open(str(tp)) as src:
            img_arr = src.read([1, 2, 3])  # (3, H, W)

        img_arr = np.transpose(img_arr, (1, 2, 0))  # (H, W, 3)

        if img_arr.dtype != np.uint8:
            if img_arr.max() > 0:
                img_arr = (img_arr.astype(np.float32) / img_arr.max() * 255).clip(0, 255).astype(np.uint8)

        cluster_map = np.load(str(cluster_path))  # (H, W) int16
        H, W = cluster_map.shape

        for cid in range(n_clusters):
            if len(cluster_patches[cid]) >= patches_per_cluster:
                continue

            ys, xs = np.where(cluster_map == cid)
            if len(ys) == 0:
                continue

            # Randomly pick positions that hold a full patch
            margin = patch_size // 2
            valid = (ys >= margin) & (ys < H - margin) & (xs >= margin) & (xs < W - margin)
            ys, xs = ys[valid], xs[valid]

            if len(ys) == 0:
                continue

            n_take = min(patches_per_cluster - len(cluster_patches[cid]), len(ys))
            chosen_idx = rng.choice(len(ys), size=n_take, replace=False)

            for i in chosen_idx:
                y, x = ys[i], xs[i]
                patch = img_arr[
                    y - margin : y + margin,
                    x - margin : x + margin,
                ]
                if patch.shape[0] == patch_size and patch.shape[1] == patch_size:
                    cluster_patches[cid].append(Image.fromarray(patch))

    return cluster_patches


def make_montage(
    cluster_patches: dict,
    n_clusters: int,
    patches_per_cluster: int = 12,
    patch_size: int = 64,
    gap: int = 4,
    label_height: int = 20,
) -> Image.Image:
    """
    Build the montage: n_clusters rows, patches_per_cluster cells per row.
    Each row starts with a "Cluster XX" label.
    """
    n_cols = patches_per_cluster
    cell_w = patch_size + gap
    cell_h = patch_size + gap
    row_h  = cell_h + label_height

    total_w = n_cols * cell_w + gap
    total_h = n_clusters * row_h + gap

    canvas = Image.new("RGB", (total_w, total_h), color=(30, 30, 30))
    draw   = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except Exception:
        font = ImageFont.load_default()

    for cid in range(n_clusters):
        y_row = gap + cid * row_h

        # Label
        draw.text((gap, y_row), f"Cluster {cid:02d}", fill=(220, 220, 220), font=font)

        patches = cluster_patches.get(cid, [])
        for j, patch_img in enumerate(patches[:n_cols]):
            x = gap + j * cell_w
            y = y_row + label_height
            canvas.paste(patch_img.resize((patch_size, patch_size)), (x, y))

    return canvas


def export_montage(
    tiles_dir: str,
    clusters_dir: str,
    out_dir: str,
    n_clusters: int,
    patches_per_cluster: int = 12,
) -> str:
    """
    Export the montage PNG and a template CSV.
    Returns: path to montage PNG
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    patches = collect_cluster_patches(
        tiles_dir=tiles_dir,
        clusters_dir=clusters_dir,
        n_clusters=n_clusters,
        patches_per_cluster=patches_per_cluster,
    )

    montage = make_montage(
        cluster_patches=patches,
        n_clusters=n_clusters,
        patches_per_cluster=patches_per_cluster,
    )

    montage_path = out_dir / "cluster_montage.png"
    montage.save(str(montage_path))
    print(f"Montage saved: {montage_path}  ({montage.size[0]}x{montage.size[1]} px)")

    # Export template CSV
    csv_path = out_dir / "cluster_names.csv"
    if not csv_path.exists():
        with open(str(csv_path), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["cluster_id", "species"])
            for cid in range(n_clusters):
                writer.writerow([cid, ""])  # Human fills in species column
        print(f"Template CSV saved: {csv_path}")
        print("Open cluster_montage.png, then fill in cluster_names.csv with species names.")
    else:
        print(f"CSV already exists: {csv_path}")

    return str(montage_path)


# ============================================================
# STEP 2: Apply the mapping
# ============================================================

def apply_mapping(csv_path: str, out_yaml_path: str, class_map: dict) -> dict:
    """
    Read the CSV -> validate the species names -> save the YAML mapping.
    Returns: dict cluster_id (int) -> class_id (int)
    """
    # Invert class map: species_name -> class_id
    name_to_id = {v: k for k, v in class_map.items()}

    mapping = {}  # cluster_id -> class_id
    errors  = []

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid     = int(row["cluster_id"])
            species = row["species"].strip()

            if not species:
                print(f"  Warning: cluster {cid} has no species - assigning 'None'")
                species = "None"

            if species not in name_to_id:
                errors.append(f"Cluster {cid}: '{species}' not in class map")
                continue

            mapping[cid] = name_to_id[species]

    if errors:
        print("\nErrors in CSV:")
        for e in errors:
            print(f"  {e}")
        print(f"\nValid species names: {list(name_to_id.keys())}")
        raise ValueError(f"{len(errors)} cluster(s) have invalid species names. Fix CSV and retry.")

    # Save YAML
    yaml_data = {
        "cluster_to_class_id": {int(k): int(v) for k, v in mapping.items()},
        "class_id_to_name": {int(k): v for k, v in class_map.items()},
    }
    with open(out_yaml_path, "w") as f:
        yaml.dump(yaml_data, f, default_flow_style=False)

    print(f"Mapping saved: {out_yaml_path}")
    print(f"  {len(mapping)} clusters mapped to {len(set(mapping.values()))} unique classes")
    return mapping


def main():
    parser = argparse.ArgumentParser(description="Cluster naming: export montage or apply CSV mapping")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--site", required=True)
    parser.add_argument("--export-montage", action="store_true",
                        help="Export cluster montage PNG + template CSV")
    parser.add_argument("--apply-mapping", action="store_true",
                        help="Read filled-in CSV and save cluster->class mapping YAML")
    parser.add_argument("--csv", default=None, help="Path to filled cluster_names.csv")
    args = parser.parse_args()

    cfg = load_config(args.config)
    c   = cfg["clustering"]

    tiles_dir    = os.path.join(cfg["paths"]["tiles_dir"], args.site, "images")
    clusters_dir = os.path.join(cfg["paths"]["clusters_dir"], args.site)
    out_dir      = clusters_dir  # save montage alongside cluster maps

    if args.export_montage:
        export_montage(
            tiles_dir=tiles_dir,
            clusters_dir=clusters_dir,
            out_dir=out_dir,
            n_clusters=c["n_clusters"],
            patches_per_cluster=c["patches_per_cluster"],
        )

    if args.apply_mapping:
        csv_path = args.csv or os.path.join(clusters_dir, "cluster_names.csv")
        out_yaml = os.path.join(clusters_dir, "cluster_class_mapping.yaml")
        apply_mapping(
            csv_path=csv_path,
            out_yaml_path=out_yaml,
            class_map=cfg["classes"],
        )
        print(f"\nNext step: python src/pseudolabel.py --config config.yaml --site {args.site}")


if __name__ == "__main__":
    main()
