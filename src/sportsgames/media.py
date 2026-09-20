from __future__ import annotations

import os
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None

from .utils import safe_filename, text


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
    fmt = text(story.get("format", "SPORTS & GAMES"))
    label_map = {
        "daily_next": "NEXT UP", "daily_past": "THE DAY IN SPORTS", "game_discovery": "GAME DISCOVERY",
        "fact": "DID YOU KNOW?", "rule_check": "RULE CHECK", "how_to_play": "HOW TO PLAY",
        "on_this_date": "ON THIS DATE", "century_ago": "100 YEARS AGO", "why": "WHY?",
        "first_last_only": "FIRST / LAST / ONLY", "then_vs_now": "THEN → NOW", "forgotten": "FORGOTTEN",
        "number": "THE STORY BEHIND THE NUMBER", "history": "SPORTS & GAMES HISTORY",
    }
    label = label_map.get(fmt, "SPORTS & GAMES")
    headline = text(story.get("headline"))[:72]
    date_anchor = text(story.get("date_anchor"))

    draw.rectangle((48, 48, width - 48, height - 48), outline=(35, 35, 35), width=3)
    draw.text((82, 82), "THE SPORTS NEWSROOM", font=_font(34, True), fill=(25, 25, 25))
    draw.text((82, 145), label, font=_font(28, True), fill=(75, 75, 75))
    # Simple wrapped headline for dependable rendering on GitHub Actions.
    words = headline.split()
    lines, current = [], ""
    font = _font(52, True)
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textbbox((0, 0), trial, font=font)[2] > width - 170:
            if current:
                lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    draw.multiline_text((82, 220), "\n".join(lines[:3]), font=font, fill=(10, 10, 10), spacing=12)
    if date_anchor:
        draw.text((82, 585), date_anchor, font=_font(28, False), fill=(70, 70, 70))
    draw.text((width - 420, 585), "@TheSportsNewsroom", font=_font(26, True), fill=(70, 70, 70))

    path = Path("/tmp") / f"sports_games_{int(time.time()*1000)}_{index}_{safe_filename(headline)}.jpg"
    background.save(path, "JPEG", quality=90, optimize=True)
    return str(path)
