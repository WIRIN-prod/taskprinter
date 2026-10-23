"""Receipt layouts: formatted documents, Markdown notes, QR labels, banners, test page."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import markdown as md
from .imaging import to_bitmap, trim_blank
from .markdown import Block as DocBlock
from .markdown import Run, Style

# ---- fonts -------------------------------------------------------------------

_DIRS = (
    "", "/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/TTF/", "/usr/share/fonts/dejavu/",
    "/usr/share/fonts/truetype/liberation/",
)
_MAC = "/System/Library/Fonts/Supplemental/"
# (family, bold, italic): candidate files - DejaVu, Liberation, Windows, macOS
_FILES = {
    ("sans", False, False): ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf", "segoeui.ttf", _MAC + "Arial.ttf"],
    ("sans", True, False): ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf",
                            _MAC + "Arial Bold.ttf"],
    ("sans", False, True): ["DejaVuSans-Oblique.ttf", "LiberationSans-Italic.ttf", "ariali.ttf", "segoeuii.ttf",
                            _MAC + "Arial Italic.ttf"],
    ("sans", True, True): ["DejaVuSans-BoldOblique.ttf", "LiberationSans-BoldItalic.ttf", "arialbi.ttf",
                           "segoeuiz.ttf", _MAC + "Arial Bold Italic.ttf"],
    ("serif", False, False): ["DejaVuSerif.ttf", "LiberationSerif-Regular.ttf", "georgia.ttf", "times.ttf",
                              _MAC + "Georgia.ttf"],
    ("serif", True, False): ["DejaVuSerif-Bold.ttf", "LiberationSerif-Bold.ttf", "georgiab.ttf", "timesbd.ttf",
                             _MAC + "Georgia Bold.ttf"],
    ("serif", False, True): ["DejaVuSerif-Italic.ttf", "LiberationSerif-Italic.ttf", "georgiai.ttf", "timesi.ttf",
                             _MAC + "Georgia Italic.ttf"],
    ("serif", True, True): ["DejaVuSerif-BoldItalic.ttf", "LiberationSerif-BoldItalic.ttf", "georgiaz.ttf",
                            "timesbi.ttf", _MAC + "Georgia Bold Italic.ttf"],
    ("mono", False, False): ["DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "consola.ttf", "cour.ttf",
                             _MAC + "Courier New.ttf"],
    ("mono", True, False): ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "consolab.ttf", "courbd.ttf",
                            _MAC + "Courier New Bold.ttf"],
    ("mono", False, True): ["DejaVuSansMono-Oblique.ttf", "LiberationMono-Italic.ttf", "consolai.ttf", "couri.ttf",
                            _MAC + "Courier New Italic.ttf"],
    ("mono", True, True): ["DejaVuSansMono-BoldOblique.ttf", "LiberationMono-BoldItalic.ttf", "consolaz.ttf",
                           "courbi.ttf", _MAC + "Courier New Bold Italic.ttf"],
}
FAMILIES = ("sans", "serif", "mono")
FONT_OVERRIDES: dict[str, str] = {}


@functools.lru_cache(maxsize=256)
def font(size: int, bold: bool = False, italic: bool = False, mono: bool = False,
         family: str = "sans") -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    family = "mono" if mono else (family if family in FAMILIES else "sans")
    candidates: list[str] = []
    if family == "sans" and not italic:
        override = FONT_OVERRIDES.get("bold" if bold else "regular")
        if override:
            candidates.append(override)
    for name in _FILES[(family, bold, italic)]:
        candidates += [name] if name.startswith("/") else [d + name for d in _DIRS]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    if italic:  # no italic face installed: upright of the same family
        return font(size, bold, False, False, family)
    if family != "sans":
        return font(size, bold, False, False, "sans")
    return ImageFont.load_default(size=size)


def load_font_overrides(regular: str, bold: str) -> None:
    for key, path in (("regular", regular), ("bold", bold)):
        if path and Path(path).exists():
            FONT_OVERRIDES[key] = path
    font.cache_clear()


# ---- formatting options --------------------------------------------------------

BORDERS = ("none", "line", "double", "dashed", "rounded")
ALIGNS = ("left", "center", "right")


@dataclass
class Format:
    """Print-wide formatting, set from the web app's Format panel."""

    font: str = "sans"
    size: int = 24
    align: str = "left"  # default for blocks without their own alignment
    line_spacing: float = 1.0  # multiplier on the font's line height
    paragraph_spacing: int = 8  # px after each paragraph
    margin: int = 10  # px left and right
    border: str = "none"
    invert: bool = False  # white on black
    timestamp: bool = True

    @classmethod
    def from_dict(cls, values: dict | None) -> "Format":
        values = values or {}
        known = {k: values[k] for k in cls.__dataclass_fields__ if k in values and values[k] is not None}
        fmt = cls(**known)
        fmt.font = fmt.font if fmt.font in FAMILIES else "sans"
        fmt.size = max(12, min(96, int(fmt.size)))
        fmt.align = fmt.align if fmt.align in ALIGNS else "left"
        fmt.line_spacing = max(0.7, min(2.5, float(fmt.line_spacing)))
        fmt.paragraph_spacing = max(0, min(60, int(fmt.paragraph_spacing)))
        fmt.margin = max(0, min(60, int(fmt.margin)))
        fmt.border = fmt.border if fmt.border in BORDERS else "none"
        fmt.invert, fmt.timestamp = bool(fmt.invert), bool(fmt.timestamp)
        return fmt

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ---- text layout ---------------------------------------------------------------

DrawFn = Callable[[ImageDraw.ImageDraw, Image.Image, int], None]
Piece = tuple[float, str, Style]  # x offset, text, style
CODE_PAD = 3
_SPLIT = re.compile(r"(\n| +)")


def wrap(text: str, fnt, max_width: int) -> list[str]:
    """Greedy word wrap by rendered pixel width; hard-breaks very long words."""
    lines: list[str] = []
    for paragraph in text.replace("\r", "").split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if fnt.getlength(candidate) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while fnt.getlength(word) > max_width:
                cut = len(word)
                while cut > 1 and fnt.getlength(word[:cut]) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _face(style: Style, size: int, family: str, mono: bool = False):
    return font(size, style.bold, style.italic, style.code or mono, family)


def _width(piece: Piece, size: int, family: str, mono: bool = False) -> float:
    pad = 2 * CODE_PAD if piece[2].code else 0
    return _face(piece[2], size, family, mono).getlength(piece[1]) + pad


def layout(runs: list[Run], size: int, max_width: int, *, family: str = "sans", keep_spaces: bool = False,
           boxes: bool = True) -> list[list[Piece]]:
    """Word-wrap styled runs into lines of positioned pieces."""
    lines: list[list[Piece]] = [[]]
    x = 0.0
    for run in runs:
        style = run.style if boxes else replace(run.style, code=False)
        face = _face(run.style, size, family)
        pad = 2 * CODE_PAD if style.code else 0
        parts = run.text.split("\n") if style.code else _SPLIT.split(run.text)
        if style.code:  # keep a code span in one box; re-insert the line breaks
            parts = [p for i, line in enumerate(parts) for p in ((["\n"] if i else []) + [line])]
        for part in parts:
            if not part:
                continue
            if part == "\n":
                lines.append([])
                x = 0.0
                continue
            if part.isspace():
                if lines[-1] or keep_spaces:
                    x += face.getlength(part)
                continue
            w = face.getlength(part) + pad
            if x + w > max_width and lines[-1]:
                lines.append([])
                x = 0.0
            while w > max_width and len(part) > 1:  # hard-break words wider than a line
                cut = len(part) - 1
                while cut > 1 and face.getlength(part[:cut]) + pad > max_width:
                    cut -= 1
                lines[-1].append((x, part[:cut], style))
                lines.append([])
                x, part = 0.0, part[cut:]
                w = face.getlength(part) + pad
            lines[-1].append((x, part, style))
            x += w
    return lines


def _draw_pieces(d: ImageDraw.ImageDraw, pieces: list[Piece], x0: float, baseline: float, size: int,
                 family: str, mono: bool = False) -> None:
    for x, text, style in pieces:
        face = _face(style, size, family, mono)
        left = x0 + x
        w = face.getlength(text)
        if style.code:
            ascent, descent = face.getmetrics()
            d.rectangle((left, baseline - ascent + 2, left + w + 2 * CODE_PAD, baseline + descent),
                        outline=0, width=1)
            left += CODE_PAD
        d.text((left, baseline), text, font=face, fill=0, anchor="ls")
        if style.strike:
            mid = baseline - size * 0.32
            d.line((left, mid, left + w, mid), fill=0, width=max(2, size // 12))
        if style.underline:
            d.line((left, baseline + 2, left + w, baseline + 2), fill=0, width=max(1, size // 14))


def _bold(runs: list[Run]) -> list[Run]:
    return [Run(r.text, replace(r.style, bold=True)) for r in runs]


# ---- receipt builder -------------------------------------------------------------

@dataclass
class Receipt:
    """Accumulates drawing blocks top to bottom, then renders to a 1-bit image."""

    width: int = 384
    margin: int = 10
    family: str = "sans"
    line_spacing: float = 1.0
    paragraph_spacing: int = 8
    blocks: list[tuple[int, DrawFn]] = field(default_factory=list)

    @property
    def inner(self) -> int:
        return self.width - 2 * self.margin

    def _add(self, height: int, draw: DrawFn) -> None:
        self.blocks.append((height, draw))

    def _line_height(self, face) -> tuple[int, int]:
        ascent, descent = face.getmetrics()
        return ascent, int(round((ascent + descent) * self.line_spacing)) + 4

    def space(self, height: int = 10) -> "Receipt":
        self._add(max(0, height), lambda d, im, y: None)
        return self

    def text(self, text: str, size: int = 24, *, bold: bool = False, align: str = "left") -> "Receipt":
        """Plain, unstyled text."""
        return self.rich([Run(text, Style(bold=bold))], size, align=align)

    def row(self, left: str, right: str, size: int = 20, *, bold: bool = False) -> "Receipt":
        face = font(size, bold, family=self.family)
        ascent, descent = face.getmetrics()
        margin, inner = self.margin, self.inner

        def draw(d, im, y):
            d.text((margin, y), left, font=face, fill=0)
            d.text((margin + inner - face.getlength(right), y), right, font=face, fill=0)

        self._add(ascent + descent + 4, draw)
        return self

    def rule(self, thickness: int = 3, dashed: bool = False, gap: int = 8) -> "Receipt":
        margin, inner = self.margin, self.inner

        def draw(d, im, y):
            mid = y + gap
            if dashed:
                for x in range(margin, margin + inner, 12):
                    d.rectangle((x, mid, min(x + 6, margin + inner), mid + thickness - 1), fill=0)
            else:
                d.rectangle((margin, mid, margin + inner, mid + thickness - 1), fill=0)

        self._add(thickness + 2 * gap, draw)
        return self

    def rich(self, runs: list[Run], size: int = 24, *, indent: int = 0, align: str = "left",
             bar: bool = False, mono: bool = False, prefix=None) -> "Receipt":
        """Styled, word-wrapped text. `prefix(draw, y, baseline)` draws a bullet or box."""
        family = self.family
        lines = layout(runs, size, self.inner - indent, family=family, keep_spaces=mono, boxes=not mono)
        ascent, line_h = self._line_height(font(size, mono=mono, family=family))
        height = line_h * len(lines)
        margin, inner = self.margin, self.inner

        def draw(d, im, y):
            if bar:
                d.rectangle((margin + indent - 14, y, margin + indent - 10, y + height - 4), fill=0)
            if prefix:
                prefix(d, y, y + ascent)
            for i, pieces in enumerate(lines):
                if not pieces:
                    continue
                used = max(_width(piece, size, family, mono) + piece[0] for piece in pieces)
                x0 = margin + indent
                if align == "center":
                    x0 += (inner - indent - used) / 2
                elif align == "right":
                    x0 += inner - indent - used
                _draw_pieces(d, pieces, x0, y + i * line_h + ascent, size, family, mono)

        self._add(height, draw)
        return self

    def checkbox(self, text: str | list[Run], size: int = 24, *, checked: bool = False,
                 indent: int = 0) -> "Receipt":
        runs = md.parse_inline(text) if isinstance(text, str) else text
        box = size - 2
        left = self.margin + indent
        ascent = font(size, family=self.family).getmetrics()[0]

        def draw_box(d, y, baseline):
            top = y + max(0, (ascent - box) // 2 + 2)
            d.rectangle((left, top, left + box, top + box), outline=0, width=3)
            if checked:
                d.line((left + 4, top + box // 2, left + box // 2, top + box - 4), fill=0, width=3)
                d.line((left + box // 2, top + box - 4, left + box - 3, top + 4), fill=0, width=3)

        self.rich(runs, size, indent=indent + box + 12, prefix=draw_box)
        return self.space(4)

    def _list_item(self, runs: list[Run], size: int, marker: str, indent: int) -> None:
        left = self.margin + indent
        if marker == "•":
            col = 22
            radius = max(3, size // 7)

            def prefix(d, y, baseline):
                cy = baseline - size * 0.35
                d.ellipse((left + 4, cy - radius, left + 4 + 2 * radius, cy + radius), fill=0)
        else:
            face = font(size, True, family=self.family)
            col = int(face.getlength("00.")) + 8

            def prefix(d, y, baseline):
                d.text((left + col - 8, baseline), marker, font=face, fill=0, anchor="rs")
        self.rich(runs, size, indent=indent + col, prefix=prefix).space(2)

    def document(self, blocks: list[DocBlock], size: int = 24, *, align: str = "left",
                 bold: bool = False) -> "Receipt":
        """Lay out parsed blocks (from the editor or from Markdown)."""
        gap = self.paragraph_spacing
        for i, block in enumerate(blocks):
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            runs = _bold(block.runs) if bold else block.runs
            where = block.align or align
            if block.kind == "heading":
                if i:
                    self.space(max(4, gap))
                hsize = {1: round(size * 1.5), 2: round(size * 1.25), 3: round(size * 1.08)}[block.level]
                self.rich(_bold(runs), hsize, align=where)
                if block.level == 1:
                    self.rule(3, gap=4)
                else:
                    self.space(4)
            elif block.kind == "para":
                self.rich(runs or [Run(" ")], size, align=where).space(gap)
            elif block.kind == "quote":
                self.rich(runs, size, indent=18, bar=True).space(gap)
            elif block.kind == "rule":
                self.rule(2, dashed=True)
            elif block.kind == "code":
                code_run = Run(block.text or " ", Style(code=True))
                self.rich([code_run], max(12, round(size * 0.8)), indent=18, bar=True, mono=True).space(gap)
            elif block.kind == "item":
                indent = block.level * 22
                if block.checked is not None:
                    self.checkbox(runs, size, checked=block.checked, indent=indent)
                else:
                    self._list_item(runs, size, block.marker, indent)
                if not nxt or nxt.kind != "item":
                    self.space(max(4, gap // 2))
        return self

    def markdown(self, text: str, size: int = 24, *, align: str = "left", bold: bool = False) -> "Receipt":
        return self.document(md.parse(text), size, align=align, bold=bold)

    def image(self, img: Image.Image, align: str = "center") -> "Receipt":
        margin, inner = self.margin, self.inner

        def draw(d, im, y):
            x = margin + (inner - img.width) // 2 if align == "center" else margin
            im.paste(img.convert("L"), (x, y))

        self._add(img.height, draw)
        return self

    def render(self) -> Image.Image:
        height = sum(h for h, _ in self.blocks) + self.margin
        canvas = Image.new("L", (self.width, max(1, height)), 255)
        draw = ImageDraw.Draw(canvas)
        y = min(self.margin, 10)
        for h, block in self.blocks:
            block(draw, canvas, y)
            y += h
        bitmap = to_bitmap(canvas, self.width, dither="threshold", threshold=128, scale=False)
        return trim_blank(bitmap, margin=min(self.margin, 10))


def _stamp(r: Receipt) -> None:
    r.space(6).rich([Run(datetime.now().strftime("%d %b %Y %H:%M"))], 16, align="center")


def finish(bitmap: Image.Image, fmt: Format) -> Image.Image:
    """Apply the frame and colour options to a rendered bitmap."""
    if fmt.border != "none":
        pad = 8
        framed = Image.new("1", (bitmap.width, bitmap.height + 2 * pad), 1)
        framed.paste(bitmap, (0, pad))
        d = ImageDraw.Draw(framed)
        box = (2, 2, framed.width - 3, framed.height - 3)
        if fmt.border == "line":
            d.rectangle(box, outline=0, width=3)
        elif fmt.border == "double":
            d.rectangle(box, outline=0, width=2)
            d.rectangle((7, 7, framed.width - 8, framed.height - 8), outline=0, width=1)
        elif fmt.border == "rounded":
            d.rounded_rectangle(box, radius=16, outline=0, width=3)
        else:  # dashed
            x0, y0, x1, y1 = box
            for x in range(x0, x1, 14):
                d.rectangle((x, y0, min(x + 7, x1), y0 + 2), fill=0)
                d.rectangle((x, y1 - 2, min(x + 7, x1), y1), fill=0)
            for y in range(y0, y1, 14):
                d.rectangle((x0, y, x0 + 2, min(y + 7, y1)), fill=0)
                d.rectangle((x1 - 2, y, x1, min(y + 7, y1)), fill=0)
        bitmap = framed
    if fmt.invert:
        bitmap = ImageChops.invert(bitmap.convert("L")).convert("1", dither=Image.Dither.NONE)
    return bitmap


def receipt_for(fmt: Format, width: int) -> Receipt:
    margin = max(fmt.margin, 16) if fmt.border != "none" else fmt.margin
    return Receipt(width, margin=margin, family=fmt.font, line_spacing=fmt.line_spacing,
                   paragraph_spacing=fmt.paragraph_spacing)


# ---- layouts ---------------------------------------------------------------------

def document(blocks: list[DocBlock], width: int = 384, fmt: Format | None = None) -> Image.Image:
    """A formatted document from the web editor."""
    fmt = fmt or Format()
    r = receipt_for(fmt, width)
    r.document(blocks, fmt.size, align=fmt.align)
    if fmt.timestamp:
        _stamp(r)
    return finish(r.render(), fmt)


def note(text: str, width: int = 384, *, title: str = "", markdown: bool = True,
         bold: bool = False, fmt: Format | None = None) -> Image.Image:
    """Typed text (CLI, API, share sheet): Markdown by default, or literal."""
    fmt = fmt or Format(size=28)
    r = receipt_for(fmt, width)
    if title:
        r.rich([Run(title, Style(bold=True))], min(fmt.size + 8, 56), align=fmt.align).rule(3, gap=6)
    if markdown:
        r.markdown(text, fmt.size, align=fmt.align, bold=bold)
    else:
        r.text(text, fmt.size, bold=bold, align=fmt.align)
    if fmt.timestamp:
        _stamp(r)
    return finish(r.render(), fmt)


def qr_label(data: str, width: int = 384, *, caption: str = "", scale: float = 1.0) -> Image.Image:
    """A QR code as large as the paper allows, with an optional Markdown caption."""
    import qrcode

    qr = qrcode.QRCode(border=0, box_size=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    modules = qr.make_image(fill_color="black", back_color="white").get_image().convert("L")
    r = Receipt(width)
    box = max(1, int((r.inner * max(0.2, min(1.0, scale))) // modules.width))
    code = modules.resize((modules.width * box, modules.height * box), Image.Resampling.NEAREST)
    r.image(code)
    if caption.strip():
        r.space(8).markdown(caption, 22 if width >= 300 else 18, align="center")
    return r.render()


def banner(text: str, width: int = 384, *, bold: bool = True, family: str = "sans") -> Image.Image:
    """Huge text printed sideways, reading along the roll."""
    text = " ".join(text.split()) or " "
    size = width - 24
    face = font(size, bold, family=family)
    while size > 40:
        ascent, descent = face.getmetrics()
        if ascent + descent <= width - 16:
            break
        size -= 8
        face = font(size, bold, family=family)
    ascent, descent = face.getmetrics()
    length = int(face.getlength(text)) + 40
    strip = Image.new("L", (length, width), 255)
    ImageDraw.Draw(strip).text((20, (width - ascent - descent) // 2), text, font=face, fill=0)
    upright = strip.rotate(-90, expand=True)  # first letter at the top of the print
    return to_bitmap(upright, width, dither="threshold", threshold=128, scale=False)


def test_page(width: int = 384, info: dict | None = None) -> Image.Image:
    r = Receipt(width)
    r.text("TASK PRINTER", 40, bold=True, align="center")
    r.text(datetime.now().strftime("%d %b %Y  %H:%M:%S"), 20, align="center").rule(4)
    for key, value in (info or {}).items():
        r.row(str(key), str(value), 18)
    r.rule(2, dashed=True)

    swatch = Image.new("L", (width - 20, 60), 255)
    d = ImageDraw.Draw(swatch)
    for i in range(16):
        x = i * (swatch.width / 16)
        d.rectangle((x, 0, x + swatch.width / 16, 59), fill=int(i * 255 / 15))
    r.image(to_bitmap(swatch, swatch.width, dither="floyd", scale=False))
    r.space(6)

    bars = Image.new("L", (width - 20, 40), 255)
    d = ImageDraw.Draw(bars)
    for i, w in enumerate((1, 2, 3, 4, 6, 8)):
        x0 = i * 60
        for x in range(x0, x0 + 50, w * 2):
            d.rectangle((x, 0, x + w - 1, 39), fill=0)
    r.image(bars).space(6)
    for family in FAMILIES:
        r.family = family  # rich() captures the family when the block is added
        r.rich([Run(f"{family}: ", Style(bold=True)), Run("Regular "), Run("Bold ", Style(bold=True)),
                Run("Italic", Style(italic=True))], 20)
    r.family = "sans"
    r.text("The quick brown fox jumps over the lazy dog.", 18)
    return r.render()
