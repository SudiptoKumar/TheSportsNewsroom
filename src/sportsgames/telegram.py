from __future__ import annotations

import html
import re
from pathlib import Path

from .config import MAX_CAPTION_CHARACTERS, MAX_RICH_CHARACTERS, TELEGRAM_CHANNEL
from .providers import telegram_call
from .utils import clamp, text


def _terms(story: dict) -> list[str]:
    return [
        text(story.get("game_or_sport")), text(story.get("date_anchor")),
        text(story.get("headline")), "DID YOU KNOW?", "NEXT UP", "THE DAY IN SPORTS",
    ]


def render_rich_html(story: dict) -> str:
    fmt = text(story.get("format"))
    labels = {
        "fact": "DID YOU KNOW?", "game_discovery": "GAME DISCOVERY", "rule_check": "RULE CHECK",
        "how_to_play": "HOW TO PLAY", "history": "SPORTS & GAMES HISTORY", "on_this_date": "ON THIS DATE",
        "century_ago": "100 YEARS AGO", "why": "WHY?", "first_last_only": "FIRST / LAST / ONLY",
        "then_vs_now": "THEN → NOW", "forgotten": "FORGOTTEN", "number": "THE STORY BEHIND THE NUMBER",
        "daily_next": "NEXT UP", "daily_past": "THE DAY IN SPORTS",
    }
    label = labels.get(fmt, "SPORTS & GAMES")
    parts = [f"<h1>{html.escape(label)}</h1>"]
    date_anchor = text(story.get("date_anchor"))
    if date_anchor:
        parts.append(f"<p><b>{html.escape(date_anchor)}</b></p>")
    parts.append(f"<h2>{html.escape(text(story.get('headline')))}</h2>")
    parts.append(f"<p>{html.escape(text(story.get('dek')))}</p>")
    if story.get("key_points"):
        parts.append("<ul>" + "".join(f"<li>{html.escape(text(x))}</li>" for x in story["key_points"][:6]) + "</ul>")
    parts.append(f"<p>{html.escape(text(story.get('body')))}</p>")
    if text(story.get("why_interesting")):
        parts.append(f"<blockquote><b>WHY IT'S INTERESTING</b><br>{html.escape(text(story.get('why_interesting')))}</blockquote>")
    sources = list(dict.fromkeys(text(u) for u in story.get("sources", []) if text(u)))
    if sources:
        links = " · ".join(f'<a href="{html.escape(u, quote=True)}">{html.escape(_source_label(u))}</a>' for u in sources[:5])
        parts.append(f"<p><b>Sources:</b> {links}</p>")
    parts.append("<p><i>@TheSportsNewsroom</i></p>")
    return "\n".join(parts)


def _source_label(url: str) -> str:
    host = re.sub(r"^www\.", "", url.split("//", 1)[-1].split("/", 1)[0])
    return host.split(":")[0]


def visible_length(html_text: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html.unescape(html_text)))


def fit_rich_html(story: dict) -> str:
    rendered = render_rich_html(story)
    if visible_length(rendered) <= MAX_RICH_CHARACTERS:
        return rendered
    compact = dict(story)
    compact["body"] = clamp(story.get("body"), 1800)
    compact["dek"] = clamp(story.get("dek"), 500)
    compact["key_points"] = list(story.get("key_points", []))[:4]
    return render_rich_html(compact)


def send_story(image_path: str, rich_html: str) -> dict:
    rich = {
        "html": rich_html,
        "media": [{"id": "sportsphoto", "media": {"type": "photo", "media": "attach://photo"}}],
        "skip_entity_detection": False,
    }
    with Path(image_path).open("rb") as photo:
        return telegram_call(
            "sendRichMessage",
            data={"chat_id": TELEGRAM_CHANNEL, "rich_message": __import__("json").dumps(rich, ensure_ascii=False)},
            files={"photo": photo},
        )


def send_photo_fallback(image_path: str, rich_html: str) -> dict:
    caption = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    caption = re.sub(r"</(?:p|h1|h2|footer|aside|blockquote|li|ul)>", "\n", caption, flags=re.I)
    caption = re.sub(r"<[^>]+>", "", caption)
    caption = html.unescape(caption)
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    if len(caption) > MAX_CAPTION_CHARACTERS:
        caption = clamp(caption, MAX_CAPTION_CHARACTERS - 3) + "..."
    with Path(image_path).open("rb") as photo:
        return telegram_call("sendPhoto", data={"chat_id": TELEGRAM_CHANNEL, "caption": caption}, files={"photo": photo})
