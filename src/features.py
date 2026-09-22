"""
features.py
-----------
Extract DINOv2 dense features from orthomosaic tiles.

DINOv2 splits the image into 14x14 px patches, one 384/768/1024-dim vector each.
That patch size is coarse for vegetation (a single plant can be a few pixels).
Fix: bilinear upsample of the feature map from (H/14 x W/14) to (H x W).

Usage:
    python src/features.py --config config.yaml --site site_A
    # Reads:  data/tiles/site_A/images/*.tif
    # Writes: data/features/site_A/*.npy  (float32, shape H x W x C)
"""

import os
import argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoImageProcessor, AutoModel
import rasterio
import yaml
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_dino(model_name: str, device: str):
    """Load DINOv2 from HuggingFace. The first run downloads ~350MB."""
    print(f"Loading DINOv2: {model_name}")
    processor = AutoImageProcessor.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name)
    model.eval().to(device)
    return processor, model


def tile_to_rgb_array(tif_path: str) -> np.ndarray:
    """
    Read a GeoTIFF tile -> numpy RGB uint8.
    Assumes band order R, G, B (bands 1,2,3).
    """
    with rasterio.open(tif_path) as src:
        data = src.read([1, 2, 3])  # (3, H, W)

    # (3, H, W) -> (H, W, 3)
    img = np.transpose(data, (1, 2, 0))

    # Normalise to [0, 255] uint8 if needed
    if img.dtype != np.uint8:
        if img.max() > 0:
            img = (img.astype(np.float32) / img.max() * 255).clip(0, 255).astype(np.uint8)

    return img


@torch.no_grad()
def extract_features_single(
    img_rgb: np.ndarray,
    processor,
    model,
    device: str,
    patch_size: int = 14,
    upsample_mode: str = "bilinear",
) -> np.ndarray:
    """
    Input:  img_rgb  np.ndarray (H, W, 3) uint8
    Output: features np.ndarray (h_patches, w_patches, C) float32
            -- features at PATCH RESOLUTION (e.g. 36x36), not upsampled to (H, W)

    Reason: upsampling to per-pixel float32 = ~800 MB/tile -> not viable on disk/RAM.
    cluster.py upsamples the CLUSTER MAP (int16) to (H, W) after assigning clusters.

    Quy trinh:
    1. Resize the image to a multiple of patch_size
    2. The processor normalises (no resize/crop) -> tensor (1, 3, H', W')
    3. Forward qua DINOv2 -> last_hidden_state (1, num_patches+1, C)
    4. Bo CLS token -> reshape ve (h_patches, w_patches, C)
    """
    H, W = img_rgb.shape[:2]
    pil_img = Image.fromarray(img_rgb)

    # DINOv2 needs a size divisible by patch_size
    H_p = max((H // patch_size) * patch_size, patch_size)
    W_p = max((W // patch_size) * patch_size, patch_size)

    pil_resized = pil_img.resize((W_p, H_p), Image.BILINEAR)

    # The image is already a multiple of patch_size -> disable the processor resize/crop
    # (by default it forces 224x224, which breaks the patch count on reshape)
    inputs = processor(
        images=pil_resized,
        return_tensors="pt",
        do_resize=False,
        do_center_crop=False,
    )
    inputs = {k: v.to(device) for k, v in inputs.items()}

    outputs = model(**inputs)

    # last_hidden_state: (1, 1 + h*w, C) - index 0 is the CLS token
    hidden = outputs.last_hidden_state  # (1, N+1, C)
    patch_tokens = hidden[:, 1:, :]     # (1, N, C)

    h_p = H_p // patch_size
    w_p = W_p // patch_size
    C = patch_tokens.shape[-1]

    # Reshape to spatial: (h_p, w_p, C) -- keeping the patch resolution
    patch_grid = patch_tokens.reshape(h_p, w_p, C)
    return patch_grid.float().cpu().numpy().astype(np.float32)


def extract_site_features(
    tiles_dir: str,
    out_dir: str,
    dino_cfg: dict,
    device: str = None,
) -> list:
    """
    Extract features for every tile of one site.
    Resume-safe: skips .npy files that already exist.
    """
    if device is None:
        if torch.cuda.is_available():
            device = "cuda"
        elif torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
    print(f"Device: {device}")

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    processor, model = load_dino(dino_cfg["model"], device)

    tile_paths = sorted(Path(tiles_dir).glob("*.tif"))
    if len(tile_paths) == 0:
        raise ValueError(f"No .tif tiles found in {tiles_dir}")

    print(f"Extracting features for {len(tile_paths)} tiles...")
    out_paths = []

    for tp in tqdm(tile_paths, desc="DINOv2"):
        out_path = out_dir / (tp.stem + ".npy")
        if out_path.exists():
            out_paths.append(str(out_path))
            continue  # resume-safe

        img = tile_to_rgb_array(str(tp))

        if img.max() == 0:
            continue  # skip nodata tiles

        features = extract_features_single(
            img_rgb=img,
            processor=processor,
            model=model,
            device=device,
            patch_size=dino_cfg.get("patch_size", 14),
            upsample_mode=dino_cfg.get("upsample_mode", "bilinear"),
        )

        np.save(str(out_path), features)
        out_paths.append(str(out_path))

    print(f"Features saved: {len(out_paths)} files in {out_dir}")
    return out_paths


def main():
    parser = argparse.ArgumentParser(description="Extract DINOv2 dense features from tiles")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--site", required=True, help="Site name (matches folder in tiles_dir)")
    parser.add_argument("--device", default=None, help="Override device: cuda/mps/cpu")
    args = parser.parse_args()

    cfg = load_config(args.config)

    tiles_dir = os.path.join(cfg["paths"]["tiles_dir"], args.site, "images")
    out_dir   = os.path.join(cfg["paths"]["features_dir"], args.site)

    dino_cfg = cfg["dino"]
    if args.device:
        dino_cfg["device"] = args.device

    extract_site_features(
        tiles_dir=tiles_dir,
        out_dir=out_dir,
        dino_cfg=dino_cfg,
        device=dino_cfg.get("device"),
    )


if __name__ == "__main__":
    main()
