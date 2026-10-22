"""Bounded image validation; imported originals are never modified."""

import io
import warnings

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_IMAGES = 4
MEDIA = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def validate_image(data: bytes) -> tuple[str, int, int]:
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Images must be no larger than 10 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in MEDIA:
                    raise ValueError("Choose a PNG, JPEG, or WebP image.")
                if im.width * im.height > MAX_PIXELS:
                    raise ValueError("Images must be no larger than 24 megapixels.")
                if getattr(im, "n_frames", 1) != 1:
                    raise ValueError(
                        "Choose a still image; animated images are unsupported."
                    )
                media, width, height = MEDIA[im.format], im.width, im.height
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                im.load()
        return media, width, height
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise ValueError("This image is damaged or cannot be decoded.") from None
