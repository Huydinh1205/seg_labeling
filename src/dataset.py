"""
dataset.py
----------
PyTorch Dataset for DeepLabV3+ / SegFormer training.

IMPORTANT: NEVER use colour augmentation.
- No hue jitter
- No saturation jitter
- No colour jitter of any kind
- Reason: Sphagnum R/G/Y can only be told apart by colour;
         D. continentis changes colour with the season -> colour is signal, not noise.

Allowed: horizontal/vertical flip, 90-degree rotation, light Gaussian noise.
"""

import os
from pathlib import Path
from typing import Optional, Callable

import numpy as np
import torch
from torch.utils.data import Dataset
import rasterio
from PIL import Image
import albumentations as A
from albumentations.pytorch import ToTensorV2


# ============================================================
# Augmentation (NO colour aug - see CLAUDE.md section 3.1)
# ============================================================

def get_train_transform(img_size: int = 512, gaussian_noise_std: float = 0.01):
    """
    Augmentations that are safe for this problem.
    None of the transforms below may change colour (HSV/RGB).
    """
    return A.Compose([
        # Geometric only
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.RandomRotate90(p=0.5),
        # Gaussian noise rat nhe de tang robustness
        # std=0.01 on [0,1] ~ +/- 2-3 grey levels on [0,255] -> acceptable
        A.GaussNoise(std_range=(0.0, max(gaussian_noise_std, 1e-4)), p=0.3),
        # Normalise with the ImageNet mean/std (matches pretrained ResNet50)
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])


def get_val_transform():
    return A.Compose([
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])


# ============================================================
# Dataset
# ============================================================

class BogSegDataset(Dataset):
    """
    Dataset read from a folder laid out as:
        <root>/images/tile_r0001_c0002.tif
        <root>/masks/tile_r0001_c0002_label.tif   (or tile_r..._c....tif)

    Args:
        root: path to the site folder (holding images/ and masks/)
        transform: albumentations transform
        ignore_index: pixel value skipped when computing the loss (default 255)
    """

    def __init__(
        self,
        root: str,
        transform: Optional[Callable] = None,
        ignore_index: int = 255,
    ):
        self.root         = Path(root)
        self.transform    = transform
        self.ignore_index = ignore_index

        img_dir  = self.root / "images"
        mask_dir = self.root / "masks"

        img_paths  = sorted(img_dir.glob("tile_*.tif"))
        self.pairs = []

        for ip in img_paths:
            # Find the matching mask: it may be tile_r...c...tif or tile_r...c..._label.tif
            stem = ip.stem
            mask_path = mask_dir / f"{stem}_label.tif"
            if not mask_path.exists():
                mask_path = mask_dir / f"{stem}.tif"
            if mask_path.exists():
                self.pairs.append((ip, mask_path))

        if len(self.pairs) == 0:
            raise ValueError(f"No image-mask pairs found in {root}")

        print(f"Dataset: {len(self.pairs)} pairs in {root}")

    def __len__(self) -> int:
        return len(self.pairs)

    def __getitem__(self, idx: int) -> dict:
        img_path, mask_path = self.pairs[idx]

        # Read the image
        with rasterio.open(str(img_path)) as src:
            img = src.read([1, 2, 3])  # (3, H, W)
        img = np.transpose(img, (1, 2, 0))  # (H, W, 3)

        # Normalise to uint8 if needed
        if img.dtype != np.uint8:
            if img.max() > 0:
                img = (img.astype(np.float32) / img.max() * 255).clip(0, 255).astype(np.uint8)
            else:
                img = np.zeros_like(img, dtype=np.uint8)

        # Read the mask
        with rasterio.open(str(mask_path)) as src:
            mask = src.read(1).astype(np.int64)  # (H, W)

        # Nodata (255 in the pseudo-labels) -> ignore_index
        mask[mask == 255] = self.ignore_index

        if self.transform:
            augmented = self.transform(image=img, mask=mask.astype(np.int32))
            img  = augmented["image"]    # tensor (3, H, W) float32
            m = augmented["mask"]        # albumentations 2.x: ToTensorV2 -> tensor; 1.x -> ndarray
            mask = m.long() if torch.is_tensor(m) else torch.from_numpy(np.asarray(m).astype(np.int64))
        else:
            img  = torch.from_numpy(img.transpose(2, 0, 1)).float() / 255.0
            mask = torch.from_numpy(mask)

        return {"image": img, "mask": mask, "stem": img_path.stem}


def build_dataloaders(
    train_root: str,
    val_root:   str,
    batch_size: int = 8,
    num_workers: int = 4,
    gaussian_noise_std: float = 0.01,
):
    """Build train/val DataLoaders."""
    from torch.utils.data import DataLoader

    train_ds = BogSegDataset(
        root=train_root,
        transform=get_train_transform(gaussian_noise_std=gaussian_noise_std),
    )
    val_ds = BogSegDataset(
        root=val_root,
        transform=get_val_transform(),
    )

    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=num_workers,
        pin_memory=True,
        drop_last=True,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=num_workers,
        pin_memory=True,
    )
    return train_loader, val_loader
