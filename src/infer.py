"""
infer.py
--------
Sliding-window inference over the full orthomosaic.
Output: GeoTIFF label raster, same CRS + transform nhu input.

Usage:
    python src/infer.py --config config.yaml \
        --ortho data/raw/site_B.tif \
        --checkpoint checkpoints/site_A/deeplabv3plus/best.pt \
        --out data/pseudo/site_B_pred.tif
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
import rasterio
from rasterio.windows import Window
import yaml
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


@torch.no_grad()
def infer_orthomosaic(
    ortho_path: str,
    model,
    device: str,
    out_path: str,
    tile_size: int = 512,
    overlap: int = 64,
    batch_size: int = 4,
    num_classes: int = 10,
) -> None:
    """
    Sliding-window inference.
    Overlap regions: average the logits (soft voting) to avoid seam artifacts.
    """
    import math
    from albumentations import Normalize
    from albumentations.pytorch import ToTensorV2
    import albumentations as A

    transform = A.Compose([
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])

    model.eval()
    stride = tile_size - overlap

    with rasterio.open(ortho_path) as src:
        W, H = src.width, src.height
        profile = src.profile.copy()
        nodata = src.nodata

    # Accumulate logits canvas (float32) + count canvas
    logit_canvas = np.zeros((num_classes, H, W), dtype=np.float32)
    count_canvas = np.zeros((H, W), dtype=np.float32)

    n_cols = math.ceil((W - overlap) / stride)
    n_rows = math.ceil((H - overlap) / stride)
    total  = n_cols * n_rows

    # Collect tiles into batches
    batch_tiles  = []  # list of (img tensor, row_off, col_off, actual_h, actual_w)

    with rasterio.open(ortho_path) as src:
        positions = []
        for ri in range(n_rows):
            for ci in range(n_cols):
                col_off = ci * stride
                row_off = ri * stride
                actual_w = min(tile_size, W - col_off)
                actual_h = min(tile_size, H - row_off)
                positions.append((row_off, col_off, actual_h, actual_w))

        for i, (row_off, col_off, actual_h, actual_w) in enumerate(
            tqdm(positions, desc="Inference")
        ):
            window = Window(col_off, row_off, actual_w, actual_h)
            data = src.read([1, 2, 3], window=window)  # (3, H, W)
            img  = np.transpose(data, (1, 2, 0))

            if img.dtype != np.uint8:
                if img.max() > 0:
                    img = (img.astype(np.float32) / img.max() * 255).clip(0, 255).astype(np.uint8)
                else:
                    img = np.zeros_like(img, dtype=np.uint8)

            # Pad if needed
            if actual_w < tile_size or actual_h < tile_size:
                pad = np.zeros((tile_size, tile_size, 3), dtype=np.uint8)
                pad[:actual_h, :actual_w] = img
                img = pad

            aug = transform(image=img)
            tensor = aug["image"].unsqueeze(0)  # (1, 3, H, W)
            batch_tiles.append((tensor, row_off, col_off, actual_h, actual_w))

            # Forward the batch when it is full, or at the end
            if len(batch_tiles) == batch_size or i == len(positions) - 1:
                imgs_batch = torch.cat([b[0] for b in batch_tiles], dim=0).to(device)
                logits_batch = model(imgs_batch)  # (B, C, H, W)
                logits_batch = logits_batch.cpu().numpy()

                for j, (_, r_off, c_off, a_h, a_w) in enumerate(batch_tiles):
                    logit_tile = logits_batch[j]  # (C, H, W)
                    logit_canvas[:, r_off:r_off + a_h, c_off:c_off + a_w] += \
                        logit_tile[:, :a_h, :a_w]
                    count_canvas[r_off:r_off + a_h, c_off:c_off + a_w] += 1.0

                batch_tiles = []

    # Chia trung binh
    count_safe = np.where(count_canvas > 0, count_canvas, 1.0)
    logit_canvas /= count_safe[np.newaxis]

    # Argmax -> label map
    pred_map = logit_canvas.argmax(axis=0).astype(np.uint8)  # (H, W)

    # Nodata mask
    if nodata is not None:
        with rasterio.open(ortho_path) as src:
            ref = src.read(1)
        pred_map[ref == nodata] = 255

    # Write the GeoTIFF
    # Drop the options that are only valid for RGB images
    for k in ("photometric", "compress", "interleave"):
        profile.pop(k, None)
    profile.update({
        "count": 1,
        "dtype": "uint8",
        "nodata": 255,
        "compress": "deflate",
    })
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(pred_map[np.newaxis])

    print(f"Prediction saved: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Sliding-window inference on orthomosaic")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--ortho", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--out", required=True, help="Output GeoTIFF path")
    parser.add_argument("--device", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)

    device = args.device or (
        "cuda" if torch.cuda.is_available() else
        "mps"  if torch.backends.mps.is_available() else "cpu"
    )
    print(f"Device: {device}")

    # Load model
    ckpt = torch.load(args.checkpoint, map_location=device)
    model_type = ckpt.get("model_type", "deeplabv3plus")

    sys.path.insert(0, str(Path(__file__).parent))
    from train import build_deeplabv3plus, build_segformer

    if model_type == "segformer":
        model = build_segformer(cfg)
    else:
        model = build_deeplabv3plus(cfg)

    model.load_state_dict(ckpt["model_state"])
    model = model.to(device)

    inf = cfg["inference"]
    infer_orthomosaic(
        ortho_path=args.ortho,
        model=model,
        device=device,
        out_path=args.out,
        tile_size=inf["tile_size"],
        overlap=inf["overlap"],
        batch_size=inf["batch_size"],
        num_classes=cfg["num_classes"],
    )


if __name__ == "__main__":
    main()
