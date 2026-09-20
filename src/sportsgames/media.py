from __future__ import annotations

import os
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont, ImageOps
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = ImageOps = None

from .config import APP_NAME, TELEGRAM_CHANNEL
from .utils import clamp, safe_filename, text


def _font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def create_visual(story: dict, index: int = 0) -> str:
    if Image is None:
        raise RuntimeError("Pillow is required to create visuals")
    width, height = 1280, 720
    background = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(background)
    category = text(story.get("format", "SPORTS & GAMES")).upper().replace("_", " ")
    headline = clamp(story.get("headline"), 58)
    date_anchor = text(story.get("date_anchor"))

    draw.rectangle((48, 48, width - 48, height - 48), outline=(30, 30, 30), width=3)
    draw.text((82, 82), "THE SPORTS NEWSROOM", font=_font(34, True), fill=(25, 25, 25))
    draw.text((82, 145), category, font=_font(30, True), fill=(75, 75, 75))
    draw.text((82, 220), headline, font=_font(52, True), fill=(10, 10, 10), spacing=8)
    if date_anchor:
        draw.text((82, 585), date_anchor, font=_font(28, False), fill=(70, 70, 70))
    draw.text((width - 420, 585), "@TheSportsNewsroom", font=_font(26, True), fill=(70, 70, 70))

    path = Path("/tmp") / f"sports_games_{int(time.time()*1000)}_{index}_{safe_filename(headline)}.jpg"
    background.save(path, "JPEG", quality=90, optimize=True)
    return str(path)
