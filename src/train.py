"""
train.py
--------
Training loop for DeepLabV3+ (or SegFormer) on pseudo-labels.

Model defaults (CLAUDE.md section 6):
    encoder:               resnet50
    encoder_weights:       imagenet
    encoder_output_stride: 8   (keeps the fine detail Celmisia needs)
    loss:                  FocalLoss gamma=2.0  (xu ly class imbalance)
    in_channels:           3 (RGB only)
    num_classes:           10

Usage:
    python src/train.py --config config.yaml --site site_A
    python src/train.py --config config.yaml --site site_A --model segformer
"""

import os
import argparse
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import segmentation_models_pytorch as smp
import yaml
from tqdm import tqdm

from dataset import BogSegDataset, get_train_transform, get_val_transform, build_dataloaders
from validate import compute_iou_per_class


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ============================================================
# Loss
# ============================================================

class MulticlassFocalLoss(nn.Module):
    """
    Multiclass focal loss, MPS-safe.
    smp.losses.FocalLoss calls target.type('torch.mps.FloatTensor') -> ValueError on Apple MPS,
    so it is implemented here: FL = (1 - pt)^gamma * CE, skipping pixels == ignore_index.
    """

    def __init__(self, gamma: float = 2.0, ignore_index: int = 255):
        super().__init__()
        self.gamma = gamma
        self.ignore_index = ignore_index

    def forward(self, logits, target):
        import torch.nn.functional as F
        ce = F.cross_entropy(
            logits, target, reduction="none", ignore_index=self.ignore_index
        )
        pt = torch.exp(-ce)
        loss = (1.0 - pt) ** self.gamma * ce
        valid = target != self.ignore_index
        if valid.any():
            return loss[valid].mean()
        return loss.sum() * 0.0


# ============================================================
# Model factory
# ============================================================

def build_deeplabv3plus(cfg: dict) -> nn.Module:
    t = cfg["training"]
    model = smp.DeepLabV3Plus(
        encoder_name=t["encoder"],
        encoder_weights=t["encoder_weights"],
        in_channels=t["in_channels"],
        classes=cfg["num_classes"],
        encoder_output_stride=t["encoder_output_stride"],  # 8 = fine detail
    )
    return model


def build_segformer(cfg: dict) -> nn.Module:
    from transformers import SegformerForSemanticSegmentation
    import torch.nn.functional as F

    class SegFormerWrapper(nn.Module):
        def __init__(self, num_classes: int):
            super().__init__()
            self.model = SegformerForSemanticSegmentation.from_pretrained(
                "nvidia/mit-b2",
                num_labels=num_classes,
                ignore_mismatched_sizes=True,
            )

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            # SegFormer output is (B, C, H/4, W/4) -> upsample to (B, C, H, W)
            out = self.model(pixel_values=x)
            logits = out.logits  # (B, num_classes, H/4, W/4)
            logits = F.interpolate(
                logits,
                size=x.shape[-2:],
                mode="bilinear",
                align_corners=False,
            )
            return logits

    return SegFormerWrapper(cfg["num_classes"])


# ============================================================
# Training
# ============================================================

def run_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer,
    criterion,
    device: str,
    train: bool = True,
) -> float:
    model.train(train)
    total_loss = 0.0

    with torch.set_grad_enabled(train):
        for batch in tqdm(loader, desc="Train" if train else "Val ", leave=False):
            images = batch["image"].to(device)   # (B, 3, H, W)
            masks  = batch["mask"].to(device)    # (B, H, W)  long

            logits = model(images)               # (B, num_classes, H, W)

            loss = criterion(logits, masks)

            if train:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()

            total_loss += loss.item()

    return total_loss / len(loader)


def train(cfg: dict, site: str, model_type: str = "deeplabv3plus"):
    t = cfg["training"]
    device = "cuda" if torch.cuda.is_available() else \
             "mps"  if torch.backends.mps.is_available() else "cpu"
    print(f"Device: {device} | Model: {model_type}")

    # Data
    train_root = os.path.join(cfg["paths"]["tiles_dir"], site)
    val_root   = cfg["paths"]["val_dir"]  # hand-labelled val tiles

    train_loader, val_loader = build_dataloaders(
        train_root=train_root,
        val_root=val_root,
        batch_size=t["batch_size"],
        num_workers=t["num_workers"],
        gaussian_noise_std=t["augmentation"]["gaussian_noise_std"],
    )

    # Model
    if model_type == "segformer":
        model = build_segformer(cfg)
    else:
        model = build_deeplabv3plus(cfg)
    model = model.to(device)

    # Loss: FocalLoss (multiclass, gamma=2.0)
    # ignore_index=255 -> nodata pixels do not contribute to the loss
    criterion = MulticlassFocalLoss(
        gamma=t["focal_gamma"],
        ignore_index=255,
    )

    # Optimizer
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=t["lr"],
        weight_decay=t["weight_decay"],
    )

    # LR scheduler: cosine annealing
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=t["epochs"], eta_min=t["lr"] * 0.01
    )

    # Checkpoints
    ckpt_dir = Path(cfg["paths"]["checkpoints_dir"]) / site / model_type
    ckpt_dir.mkdir(parents=True, exist_ok=True)

    best_miou = 0.0

    for epoch in range(1, t["epochs"] + 1):
        train_loss = run_epoch(model, train_loader, optimizer, criterion, device, train=True)
        scheduler.step()

        lr_now = optimizer.param_groups[0]["lr"]
        print(f"Epoch {epoch:3d}/{t['epochs']}  train_loss={train_loss:.4f}  lr={lr_now:.2e}")

        if epoch % t["val_interval"] == 0 or epoch == t["epochs"]:
            val_loss = run_epoch(model, val_loader, optimizer, criterion, device, train=False)

            # Per-class IoU
            iou_dict = compute_iou_per_class(
                model=model,
                loader=val_loader,
                num_classes=cfg["num_classes"],
                device=device,
                ignore_index=255,
            )
            import math
            _valid_ious = [v for v in iou_dict.values() if not math.isnan(v)]
            miou = float(sum(_valid_ious) / len(_valid_ious)) if _valid_ious else 0.0

            print(f"  val_loss={val_loss:.4f}  mIoU={miou:.4f}")
            for cid, iou_val in iou_dict.items():
                name = cfg["classes"].get(cid, str(cid))
                print(f"    Class {cid:2d} {name:<30s}: IoU={iou_val:.4f}")

            if miou > best_miou:
                best_miou = miou
                ckpt_path = ckpt_dir / "best.pt"
                torch.save({
                    "epoch": epoch,
                    "model_state": model.state_dict(),
                    "optimizer_state": optimizer.state_dict(),
                    "miou": miou,
                    "model_type": model_type,
                    "cfg": cfg,
                }, str(ckpt_path))
                print(f"  >> Best checkpoint saved: {ckpt_path}  (mIoU={miou:.4f})")

    print(f"\nTraining done. Best mIoU: {best_miou:.4f}")
    print(f"Checkpoints: {ckpt_dir}")


def main():
    parser = argparse.ArgumentParser(description="Train DeepLabV3+ or SegFormer on pseudo-labels")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--site", required=True, help="Site name (tiles must be ready)")
    parser.add_argument("--model", default="deeplabv3plus",
                        choices=["deeplabv3plus", "segformer"],
                        help="Model architecture")
    args = parser.parse_args()

    cfg = load_config(args.config)
    train(cfg, site=args.site, model_type=args.model)


if __name__ == "__main__":
    main()
