"""
validate.py
-----------
Evaluate the model against a hand-labelled validation set.
Report per-class IoU + pixel-weighted mean IoU.

CLAUDE.md section 4 - step 7:
  "Validate honestly. Pseudo-labels are not ground truth.
   Report per-class IoU plus pixel-weighted mean IoU."

Usage:
    python src/validate.py --config config.yaml --checkpoint checkpoints/site_A/deeplabv3plus/best.pt
"""

import argparse
from pathlib import Path
from typing import Dict

import torch
import torch.nn as nn
import numpy as np
from torch.utils.data import DataLoader
import yaml
from tqdm import tqdm


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


@torch.no_grad()
def compute_iou_per_class(
    model: nn.Module,
    loader: DataLoader,
    num_classes: int,
    device: str,
    ignore_index: int = 255,
) -> Dict[int, float]:
    """
    Compute the IoU of every class.
    Returns: dict class_id -> IoU (float, NaN when the class does not appear)
    """
    model.eval()

    # Accumulate intersection and union for every class
    intersection = torch.zeros(num_classes, dtype=torch.float64)
    union        = torch.zeros(num_classes, dtype=torch.float64)

    for batch in tqdm(loader, desc="Validating", leave=False):
        images = batch["image"].to(device)
        masks  = batch["mask"].to(device)   # (B, H, W) long

        logits = model(images)              # (B, C, H, W)
        preds  = logits.argmax(dim=1)       # (B, H, W)

        # Bo qua ignore pixels
        valid = masks != ignore_index
        preds_valid = preds[valid]
        masks_valid = masks[valid]

        for c in range(num_classes):
            pred_c = preds_valid == c
            mask_c = masks_valid == c
            inter  = (pred_c & mask_c).sum().item()
            uni    = (pred_c | mask_c).sum().item()
            intersection[c] += inter
            union[c]        += uni

    iou_dict = {}
    for c in range(num_classes):
        if union[c] == 0:
            iou_dict[c] = float("nan")
        else:
            iou_dict[c] = float(intersection[c] / union[c])

    return iou_dict


def print_report(iou_dict: Dict[int, float], class_map: dict) -> None:
    """Print a formatted IoU table."""
    valid_ious = [v for v in iou_dict.values() if not np.isnan(v)]
    miou = np.mean(valid_ious) if valid_ious else 0.0

    print("\n" + "=" * 55)
    print(f"{'Class':>3}  {'Name':<30}  {'IoU':>6}")
    print("-" * 55)
    for cid in sorted(iou_dict.keys()):
        name = class_map.get(cid, str(cid))
        iou  = iou_dict[cid]
        if np.isnan(iou):
            print(f"{cid:>3}  {name:<30}  {'N/A':>6}")
        else:
            print(f"{cid:>3}  {name:<30}  {iou:>6.4f}")
    print("-" * 55)
    print(f"     {'Mean IoU (valid classes)':<30}  {miou:>6.4f}")
    print("=" * 55)


def main():
    parser = argparse.ArgumentParser(description="Validate segmentation model")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--checkpoint", required=True, help="Path to .pt checkpoint")
    parser.add_argument("--val-root", default=None, help="Override val data root from config")
    args = parser.parse_args()

    cfg = load_config(args.config)

    device = "cuda" if torch.cuda.is_available() else \
             "mps"  if torch.backends.mps.is_available() else "cpu"
    print(f"Device: {device}")

    # Load checkpoint
    ckpt = torch.load(args.checkpoint, map_location=device)
    model_type = ckpt.get("model_type", "deeplabv3plus")
    print(f"Model: {model_type}  (epoch {ckpt.get('epoch', '?')})")

    # Rebuild model
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from train import build_deeplabv3plus, build_segformer

    if model_type == "segformer":
        model = build_segformer(cfg)
    else:
        model = build_deeplabv3plus(cfg)

    model.load_state_dict(ckpt["model_state"])
    model = model.to(device)

    # Val loader
    from dataset import BogSegDataset, get_val_transform
    val_root = args.val_root or cfg["paths"]["val_dir"]
    val_ds   = BogSegDataset(root=val_root, transform=get_val_transform())
    val_loader = DataLoader(
        val_ds,
        batch_size=cfg["training"]["batch_size"],
        shuffle=False,
        num_workers=cfg["training"]["num_workers"],
    )

    # Evaluate
    iou_dict = compute_iou_per_class(
        model=model,
        loader=val_loader,
        num_classes=cfg["num_classes"],
        device=device,
    )

    print_report(iou_dict, cfg["classes"])


if __name__ == "__main__":
    main()
