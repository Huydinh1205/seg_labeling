"""
tiling.py
---------
Cut an orthomosaic GeoTIFF into 512x512 tiles with 64px overlap.
Keeps the georeference of every tile.
Uses rasterio windowed reads - NOT gdal2tiles (that one loses the CRS).

Usage:
    python src/tiling.py --config config.yaml --ortho data/raw/site_A.tif
    python src/tiling.py --config config.yaml --ortho data/raw/site_A.tif --mask data/pseudo/site_A_labels.tif
"""

import os
import math
import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.transform import from_bounds
import yaml
from tqdm import tqdm


def load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


def tile_orthomosaic(
    ortho_path: str,
    out_dir: str,
    tile_size: int = 512,
    overlap: int = 64,
    min_valid_fraction: float = 0.1,
    mask_path: str = None,
    mask_out_dir: str = None,
) -> list[str]:
    """
    Cut the orthomosaic into tiles.
    If mask_path is given, the mask is cut with exactly the same scheme (-r near).

    Returns:
        list of output tile paths
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if mask_out_dir:
        Path(mask_out_dir).mkdir(parents=True, exist_ok=True)

    stride = tile_size - overlap  # step between two tiles

    tile_paths = []

    with rasterio.open(ortho_path) as src:
        W, H = src.width, src.height
        profile = src.profile.copy()
        nodata = src.nodata

        # Number of tiles along each axis
        n_cols = math.ceil((W - overlap) / stride)
        n_rows = math.ceil((H - overlap) / stride)
        total = n_cols * n_rows

        print(f"Orthomosaic: {W}x{H} px | Tiles: {n_rows} rows x {n_cols} cols = {total}")

        mask_src = None
        if mask_path:
            mask_src = rasterio.open(mask_path)

        with tqdm(total=total, desc="Tiling") as pbar:
            for row_idx in range(n_rows):
                for col_idx in range(n_cols):

                    # Pixel coordinates (col, row) of the tile top-left corner
                    col_off = col_idx * stride
                    row_off = row_idx * stride

                    # Clamp so we never run past the image bounds
                    actual_w = min(tile_size, W - col_off)
                    actual_h = min(tile_size, H - row_off)

                    window = Window(col_off, row_off, actual_w, actual_h)
                    data = src.read(window=window)  # (bands, H, W)

                    # Check the nodata ratio
                    if nodata is not None:
                        valid_mask = (data[0] != nodata)
                        if valid_mask.mean() < min_valid_fraction:
                            pbar.update(1)
                            continue

                    # Pad the tile when it sits on the image border
                    if actual_w < tile_size or actual_h < tile_size:
                        pad = np.zeros(
                            (data.shape[0], tile_size, tile_size),
                            dtype=data.dtype
                        )
                        pad[:, :actual_h, :actual_w] = data
                        data = pad

                    # The exact transform for this tile
                    tile_transform = src.window_transform(window)

                    tile_profile = profile.copy()
                    tile_profile.update({
                        "width": tile_size,
                        "height": tile_size,
                        "transform": tile_transform,
                        "count": src.count,
                    })

                    tile_name = f"tile_r{row_idx:04d}_c{col_idx:04d}.tif"
                    tile_path = out_dir / tile_name

                    with rasterio.open(tile_path, "w", **tile_profile) as dst:
                        dst.write(data)

                    tile_paths.append(str(tile_path))

                    # Cut the mask with the identical window (nearest neighbour, no interpolation)
                    if mask_src is not None and mask_out_dir:
                        mask_data = mask_src.read(window=window)
                        if actual_w < tile_size or actual_h < tile_size:
                            mask_pad = np.zeros(
                                (mask_data.shape[0], tile_size, tile_size),
                                dtype=mask_data.dtype
                            )
                            mask_pad[:, :actual_h, :actual_w] = mask_data
                            mask_data = mask_pad

                        mask_profile = mask_src.profile.copy()
                        mask_profile.update({
                            "width": tile_size,
                            "height": tile_size,
                            "transform": tile_transform,
                            "count": mask_src.count,
                        })
                        mask_tile_path = Path(mask_out_dir) / tile_name
                        with rasterio.open(mask_tile_path, "w", **mask_profile) as mdst:
                            mdst.write(mask_data)

                    pbar.update(1)

        if mask_src is not None:
            mask_src.close()

    print(f"Saved {len(tile_paths)} tiles to {out_dir}")
    return tile_paths


def reconstruct_orthomosaic(
    tiles_dir: str,
    reference_ortho: str,
    out_path: str,
    band: int = 1,
    dtype=None,
) -> None:
    """
    Stitch the tiles back into an orthomosaic (used after inference).
    Tiles overlap - the last tile written to a position wins.
    For label rasters (categorical) - no blending.
    """
    tiles_dir = Path(tiles_dir)
    tile_paths = sorted(tiles_dir.glob("tile_r*.tif"))

    with rasterio.open(reference_ortho) as ref:
        out_profile = ref.profile.copy()
        H, W = ref.height, ref.width

    if dtype:
        out_profile["dtype"] = dtype
    out_profile["count"] = 1

    canvas = np.zeros((H, W), dtype=out_profile["dtype"])

    for tp in tqdm(tile_paths, desc="Reconstructing"):
        # Parse row/col out of the filename: tile_r0003_c0007.tif
        stem = Path(tp).stem
        parts = stem.split("_")
        row_idx = int(parts[1][1:])
        col_idx = int(parts[2][1:])

        with rasterio.open(tp) as tsrc:
            tile_data = tsrc.read(band)
            tile_h, tile_w = tile_data.shape

        stride = tile_h - 64  # recover the stride (tile_size - overlap)
        row_off = row_idx * stride
        col_off = col_idx * stride

        # Clamp
        dst_h = min(tile_h, H - row_off)
        dst_w = min(tile_w, W - col_off)

        if dst_h <= 0 or dst_w <= 0:
            continue

        canvas[row_off:row_off + dst_h, col_off:col_off + dst_w] = \
            tile_data[:dst_h, :dst_w]

    with rasterio.open(out_path, "w", **out_profile) as dst:
        dst.write(canvas[np.newaxis])

    print(f"Reconstructed: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Tile orthomosaic into 512x512 patches")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ortho", required=True, help="Path to input orthomosaic GeoTIFF")
    parser.add_argument("--mask", default=None, help="Path to label mask GeoTIFF (optional)")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Site name = the stem of the ortho filename
    site_name = Path(args.ortho).stem
    tiles_out = os.path.join(cfg["paths"]["tiles_dir"], site_name, "images")
    masks_out = None

    if args.mask:
        masks_out = os.path.join(cfg["paths"]["tiles_dir"], site_name, "masks")

    t = cfg["tiling"]
    tile_orthomosaic(
        ortho_path=args.ortho,
        out_dir=tiles_out,
        tile_size=t["tile_size"],
        overlap=t["overlap"],
        min_valid_fraction=t["min_valid_fraction"],
        mask_path=args.mask,
        mask_out_dir=masks_out,
    )


if __name__ == "__main__":
    main()
