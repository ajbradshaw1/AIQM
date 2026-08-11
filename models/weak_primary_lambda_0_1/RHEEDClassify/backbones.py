"""Shared image-backbone construction for Classifier1 and Classifier2."""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision.models as models


# All encoders expose 512-dimensional features so temporal heads remain
# directly comparable across backbone families.
BACKBONE_DIMS = {
    "resnet18": 512,
    "resnet34": 512,
    "convnext_tiny": 512,
    "convnextv2_nano_fcmae_frozen": 512,
    "dinov2_vits14_frozen": 512,
    "dinov2_vits14_pretrained_zeropad512": 512,
    "dinov2_vits14_last2": 512,
    "dinov2_vits14_frozen_stn": 512,
}


class ConvNeXtTinyEncoder(nn.Module):
    """ConvNeXt-Tiny feature extractor with a learned 768 -> 512 adapter."""

    def __init__(self, imagenet_init: bool = False) -> None:
        super().__init__()
        weights = (
            models.ConvNeXt_Tiny_Weights.DEFAULT
            if imagenet_init
            else None
        )
        backbone = models.convnext_tiny(weights=weights)
        self.features = backbone.features
        self.avgpool = backbone.avgpool
        self.norm = backbone.classifier[0]
        self.flatten = backbone.classifier[1]
        self.adapter = nn.Linear(768, BACKBONE_DIMS["convnext_tiny"])

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        features = self.features(image)
        features = self.avgpool(features)
        features = self.norm(features)
        features = self.flatten(features)
        return self.adapter(features)


class FCMAEConvNeXtV2NanoEncoder(nn.Module):
    """Frozen ConvNeXtV2-Nano FCMAE encoder with a 640 -> 512 adapter."""

    MODEL_NAME = "convnextv2_nano.fcmae"

    def __init__(self, pretrained: bool = False) -> None:
        super().__init__()
        try:
            import timm
        except ImportError as error:
            raise ImportError(
                "FCMAE backbones require timm; install it with "
                "`python -m pip install timm`."
            ) from error

        self.backbone = timm.create_model(
            self.MODEL_NAME,
            pretrained=pretrained,
            num_classes=0,
        )
        self.backbone.requires_grad_(False)
        self.adapter = nn.Linear(
            self.backbone.num_features,
            BACKBONE_DIMS["convnextv2_nano_fcmae_frozen"],
        )
        self.register_buffer(
            "input_mean",
            torch.tensor((0.485, 0.456, 0.406)).view(1, 3, 1, 1),
            persistent=False,
        )
        self.register_buffer(
            "input_std",
            torch.tensor((0.229, 0.224, 0.225)).view(1, 3, 1, 1),
            persistent=False,
        )

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        image_01 = image * 0.25 + 0.5
        image = (image_01 - self.input_mean) / self.input_std
        return self.adapter(self.backbone(image))


class DINOv2SmallEncoder(nn.Module):
    """DINOv2 ViT-S/14 with a 384 -> 512 adapter.

    Inputs use the repository's historical 0.5/0.25 normalization. The wrapper
    converts them back to [0, 1] before applying the ImageNet normalization
    expected by the pretrained DINOv2 weights.
    """

    MODEL_NAME = "vit_small_patch14_dinov2.lvd142m"

    def __init__(
        self,
        pretrained: bool = False,
        trainable_blocks: int = 0,
    ) -> None:
        super().__init__()
        try:
            import timm
        except ImportError as error:
            raise ImportError(
                "DINOv2 backbones require timm; install it with "
                "`python -m pip install timm`."
            ) from error

        self.backbone = timm.create_model(
            self.MODEL_NAME,
            pretrained=pretrained,
            num_classes=0,
            img_size=224,
        )
        self.adapter = nn.Linear(384, BACKBONE_DIMS["dinov2_vits14_frozen"])
        self.register_buffer(
            "input_mean",
            torch.tensor((0.485, 0.456, 0.406)).view(1, 3, 1, 1),
            persistent=False,
        )
        self.register_buffer(
            "input_std",
            torch.tensor((0.229, 0.224, 0.225)).view(1, 3, 1, 1),
            persistent=False,
        )
        self._set_trainable_blocks(trainable_blocks)

    def _set_trainable_blocks(self, trainable_blocks: int) -> None:
        if not 0 <= trainable_blocks <= len(self.backbone.blocks):
            raise ValueError(
                "trainable_blocks must be between 0 and "
                f"{len(self.backbone.blocks)}"
            )
        self.backbone.requires_grad_(False)
        if trainable_blocks:
            for block in self.backbone.blocks[-trainable_blocks:]:
                block.requires_grad_(True)
            self.backbone.norm.requires_grad_(True)

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        image_01 = image * 0.25 + 0.5
        image = (image_01 - self.input_mean) / self.input_std
        return self.adapter(self.backbone(image))


class PretrainedDINOv2SmallZeroPadEncoder(nn.Module):
    """Frozen pretrained DINOv2 ViT-S/14 with no locally fit adapter.

    The pretrained backbone emits 384 features.  A deterministic zero pad
    extends the final dimension to the repository-wide 512-D interface.  The
    padding has no parameters, so a registered checkpoint contains only
    ``backbone.*`` tensors and cannot leak locally trained adapter weights.
    """

    MODEL_NAME = "vit_small_patch14_dinov2.lvd142m"
    RAW_FEATURE_DIM = 384
    OUTPUT_DIM = 512
    FEATURE_PROJECTION = "zero_pad_384_to_512"

    def __init__(
        self,
        pretrained: bool = False,
        *,
        _backbone: nn.Module | None = None,
    ) -> None:
        super().__init__()
        if _backbone is None:
            try:
                import timm
            except ImportError as error:
                raise ImportError(
                    "DINOv2 backbones require timm; install it with "
                    "`python -m pip install timm`."
                ) from error
            _backbone = timm.create_model(
                self.MODEL_NAME,
                pretrained=pretrained,
                num_classes=0,
                img_size=224,
            )
        feature_count = getattr(_backbone, "num_features", self.RAW_FEATURE_DIM)
        if int(feature_count) != self.RAW_FEATURE_DIM:
            raise ValueError(
                "DINOv2 ViT-S/14 backbone must emit exactly 384 features."
            )
        self.backbone = _backbone
        self.backbone.requires_grad_(False)
        self.backbone.eval()
        self.register_buffer(
            "input_mean",
            torch.tensor((0.485, 0.456, 0.406)).view(1, 3, 1, 1),
            persistent=False,
        )
        self.register_buffer(
            "input_std",
            torch.tensor((0.229, 0.224, 0.225)).view(1, 3, 1, 1),
            persistent=False,
        )

    def train(self, mode: bool = True) -> "PretrainedDINOv2SmallZeroPadEncoder":
        super().train(mode)
        # The wrapper may live inside a training module, but this backbone is
        # always a fixed pretrained feature extractor.
        self.backbone.eval()
        return self

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        image_01 = image * 0.25 + 0.5
        image = (image_01 - self.input_mean) / self.input_std
        raw = self.backbone(image)
        if raw.ndim != 2 or raw.shape[1] != self.RAW_FEATURE_DIM:
            raise ValueError(
                "DINOv2 ViT-S/14 backbone emitted an unexpected feature shape."
            )
        return F.pad(raw, (0, self.OUTPUT_DIM - self.RAW_FEATURE_DIM))


class BoundedDenseSpatialTransformer(nn.Module):
    """Small identity-initialized non-affine image canonicalizer.

    The localizer predicts a smooth 4x4 displacement field. Bicubic
    interpolation turns it into a dense sampling grid while ``tanh`` bounds
    motion to a small fraction of the detector width/height.
    """

    def __init__(self, max_displacement: float = 0.08) -> None:
        super().__init__()
        self.max_displacement = float(max_displacement)
        self.localizer = nn.Sequential(
            nn.Conv2d(3, 16, kernel_size=5, stride=2, padding=2),
            nn.GELU(),
            nn.Conv2d(16, 32, kernel_size=3, stride=2, padding=1),
            nn.GELU(),
            nn.Conv2d(32, 64, kernel_size=3, stride=2, padding=1),
            nn.GELU(),
            nn.AdaptiveAvgPool2d((4, 4)),
        )
        self.flow_head = nn.Conv2d(64, 2, kernel_size=1)
        nn.init.zeros_(self.flow_head.weight)
        nn.init.zeros_(self.flow_head.bias)

    def displacement_field(self, image: torch.Tensor) -> torch.Tensor:
        """Return the bounded dense flow in normalized grid coordinates."""

        _, _, height, width = image.shape
        coarse_flow = self.flow_head(self.localizer(image))
        flow = F.interpolate(
            coarse_flow,
            size=(height, width),
            mode="bicubic",
            align_corners=False,
        )
        return torch.tanh(flow) * self.max_displacement

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        batch_size = image.shape[0]
        flow = self.displacement_field(image)
        identity = torch.eye(
            2,
            3,
            device=image.device,
            dtype=image.dtype,
        ).unsqueeze(0).expand(batch_size, -1, -1)
        grid = F.affine_grid(identity, image.shape, align_corners=False)
        grid = grid + flow.permute(0, 2, 3, 1)
        return F.grid_sample(
            image,
            grid,
            mode="bilinear",
            padding_mode="border",
            align_corners=False,
        )


class SpatiallyCanonicalizedDINOv2Encoder(nn.Module):
    """Bounded dense spatial transformer followed by frozen DINOv2."""

    def __init__(self, pretrained: bool = False) -> None:
        super().__init__()
        self.spatial_transformer = BoundedDenseSpatialTransformer()
        self.encoder = DINOv2SmallEncoder(
            pretrained=pretrained,
            trainable_blocks=0,
        )

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        return self.encoder(self.spatial_transformer(image))


def build_encoder(
    backbone_name: str = "resnet18",
    imagenet_init: bool = False,
) -> tuple[nn.Module, int]:
    """Build an encoder with a stable 512-dimensional output contract."""

    if backbone_name not in BACKBONE_DIMS:
        raise ValueError(
            f"Unsupported backbone {backbone_name!r}; "
            f"choose from {sorted(BACKBONE_DIMS)}"
        )
    if backbone_name == "convnext_tiny":
        return (
            ConvNeXtTinyEncoder(imagenet_init=imagenet_init),
            BACKBONE_DIMS[backbone_name],
        )
    if backbone_name == "convnextv2_nano_fcmae_frozen":
        return (
            FCMAEConvNeXtV2NanoEncoder(pretrained=imagenet_init),
            BACKBONE_DIMS[backbone_name],
        )
    if backbone_name == "dinov2_vits14_pretrained_zeropad512":
        return (
            PretrainedDINOv2SmallZeroPadEncoder(
                pretrained=imagenet_init,
            ),
            BACKBONE_DIMS[backbone_name],
        )
    if backbone_name.startswith("dinov2_vits14_"):
        if backbone_name.endswith("_stn"):
            return (
                SpatiallyCanonicalizedDINOv2Encoder(
                    pretrained=imagenet_init,
                ),
                BACKBONE_DIMS[backbone_name],
            )
        trainable_blocks = 2 if backbone_name.endswith("_last2") else 0
        return (
            DINOv2SmallEncoder(
                pretrained=imagenet_init,
                trainable_blocks=trainable_blocks,
            ),
            BACKBONE_DIMS[backbone_name],
        )

    constructor = getattr(models, backbone_name)
    if imagenet_init:
        weights_enum = getattr(
            models,
            f"{backbone_name.replace('resnet', 'ResNet')}_Weights",
        )
        backbone = constructor(weights=weights_enum.DEFAULT)
    else:
        backbone = constructor(weights=None)
    encoder = nn.Sequential(*list(backbone.children())[:-1])
    return encoder, BACKBONE_DIMS[backbone_name]
