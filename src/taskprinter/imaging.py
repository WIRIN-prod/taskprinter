"""Turn arbitrary images into 1-bit printer-width bitmaps."""

from __future__ import annotations

from PIL import Image, ImageEnhance, ImageOps

Image.MAX_IMAGE_PIXELS = 60_000_000  # refuse decompression bombs

DITHER_MODES = ("atkinson", "floyd", "ordered", "threshold")


def flatten(image: Image.Image) -> Image.Image:
    """Apply EXIF rotation and composite transparency onto white."""
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, rgba)
    return image.convert("L")


def _bayer(gray: Image.Image) -> Image.Image:
    matrix = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)
    w, h = gray.size
    src = gray.load()
    out = Image.new("1", (w, h), 1)
    dst = out.load()
    for y in range(h):
        row = (y & 3) * 4
        for x in range(w):
            if src[x, y] < (matrix[row + (x & 3)] + 0.5) * 16:
                dst[x, y] = 0
    return out


def _atkinson(gray: Image.Image) -> Image.Image:
    """Atkinson dithering: spreads 6/8 of the error, keeps thermal prints crisp."""
    w, h = gray.size
    buf = [float(v) for v in gray.tobytes()]
    out = bytearray(w * h)
    spread = ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2))
    for y in range(h):
        base = y * w
        for x in range(w):
            i = base + x
            old = buf[i]
            new = 255 if old >= 128 else 0
            out[i] = new
            err = (old - new) / 8
            if err:
                for dx, dy in spread:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and ny < h:
                        buf[ny * w + nx] += err
    return Image.frombytes("L", (w, h), bytes(out)).convert("1", dither=Image.Dither.NONE)


def to_bitmap(
    image: Image.Image,
    width: int,
    *,
    dither: str = "atkinson",
    threshold: int = 150,
    brightness: float = 1.0,
    contrast: float = 1.0,
    rotate: bool = False,
    scale: bool = True,
    autolevel: bool = False,
) -> Image.Image:
    """Return a mode "1" image exactly `width` pixels wide (black = 0)."""
    gray = flatten(image)
    if rotate and gray.width > gray.height:
        gray = gray.rotate(90, expand=True)

    if scale or gray.width > width:
        height = max(1, round(gray.height * width / gray.width))
        gray = gray.resize((width, height), Image.Resampling.LANCZOS)
    if gray.width < width:
        canvas = Image.new("L", (width, gray.height), 255)
        canvas.paste(gray, ((width - gray.width) // 2, 0))
        gray = canvas

    if autolevel:
        gray = ImageOps.autocontrast(gray, cutoff=1)
    if brightness != 1.0:
        gray = ImageEnhance.Brightness(gray).enhance(brightness)
    if contrast != 1.0:
        gray = ImageEnhance.Contrast(gray).enhance(contrast)

    if dither == "atkinson":
        return _atkinson(gray)
    if dither == "floyd":
        return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)
    if dither == "ordered":
        return _bayer(gray)
    return gray.point(lambda p: 255 if p >= threshold else 0).convert("1", dither=Image.Dither.NONE)


def bitmap_rows(bitmap: Image.Image, *, mirror: bool = False) -> list[bytes]:
    """Pack a mode "1" image into rows of bytes (MSB-left, 1 = black)."""
    if bitmap.mode != "1":
        bitmap = bitmap.convert("1", dither=Image.Dither.NONE)
    if mirror:
        bitmap = bitmap.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if bitmap.width % 8:
        raise ValueError("bitmap width must be a multiple of 8")
    stride = bitmap.width // 8
    data = bytes(b ^ 0xFF for b in bitmap.tobytes())  # Pillow: 1 = white
    return [data[i:i + stride] for i in range(0, len(data), stride)]


def trim_blank(bitmap: Image.Image, margin: int = 8) -> Image.Image:
    """Crop blank rows from the bottom of a rendered bitmap."""
    bbox = ImageOps.invert(bitmap.convert("L")).getbbox()
    if not bbox:
        return bitmap.crop((0, 0, bitmap.width, 1))
    bottom = min(bitmap.height, bbox[3] + margin)
    return bitmap.crop((0, 0, bitmap.width, bottom))
