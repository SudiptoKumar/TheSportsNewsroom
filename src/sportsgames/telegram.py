from __future__ import annotations

import html
import json
import re
from pathlib import Path

from .config import MAX_CAPTION_CHARACTERS, MAX_RICH_CHARACTERS, TELEGRAM_CHANNEL
from .providers import telegram_call
from .utils import clamp, text


def _source_label(url: str) -> str:
    host = re.sub(r"^www\.", "", url.split("//", 1)[-1].split("/", 1)[0])
    return host.split(":")[0]


def _event_groups(story: dict) -> list[tuple[str, list[dict]]]:
    groups = {}
    for event in story.get("events", []):
        sport = text(event.get("sport")) or "Other"
        groups.setdefault(sport, []).append(event)
    return sorted(groups.items(), key=lambda item: max(int(e.get("importance", 0) or 0) for e in item[1]), reverse=True)


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
        parts.append(f"<h2>{html.escape(date_anchor)}</h2>")
    parts.append(f"<p><b>{html.escape(text(story.get('headline')))}</b></p>")
    parts.append(f"<p>{html.escape(text(story.get('dek')))}</p>")

    if fmt in {"daily_next", "daily_past"}:
        events = story.get("events", [])
        spotlight = events[:6]
        if spotlight:
            parts.append("<h2>SPOTLIGHT</h2>")
            for e in spotlight:
                pieces = [f"<b>{html.escape(text(e.get('sport')))}</b>", html.escape(text(e.get('event')))]
                meta = " · ".join(x for x in [text(e.get("competition")), text(e.get("stage")), text(e.get("time_utc")), text(e.get("location"))] if x)
                if meta:
                    pieces.append(html.escape(meta))
                if text(e.get("reason")):
                    pieces.append(html.escape(text(e.get("reason"))))
                parts.append("<p>" + "<br>".join(pieces) + "</p>")
        if events:
            parts.append("<h2>BY SPORT</h2>")
            for sport, group in _event_groups(story):
                parts.append(f"<p><b>{html.escape(sport.upper())}</b></p>")
                lines = []
                for e in group:
                    meta = " · ".join(x for x in [text(e.get("time_utc")), text(e.get("competition")), text(e.get("stage"))] if x)
                    suffix = f" · {html.escape(meta)}" if meta else ""
                    lines.append(f"<li>{html.escape(text(e.get('event')))}{suffix}</li>")
                parts.append("<ul>" + "".join(lines) + "</ul>")
    else:
        if story.get("key_points"):
            parts.append("<ul>" + "".join(f"<li>{html.escape(text(x))}</li>" for x in story["key_points"][:6]) + "</ul>")
        parts.append(f"<p>{html.escape(text(story.get('body')))}</p>")
        if text(story.get("why_interesting")):
            parts.append(f"<blockquote><b>WHY IT'S INTERESTING</b><br>{html.escape(text(story.get('why_interesting')))}</blockquote>")

    sources = list(dict.fromkeys(text(u) for u in story.get("sources", []) if text(u)))
    if sources:
        links = " · ".join(f'<a href="{html.escape(u, quote=True)}">{html.escape(_source_label(u))}</a>' for u in sources[:8])
        parts.append(f"<p><b>Sources:</b> {links}</p>")
    parts.append("<p><i>@TheSportsNewsroom</i></p>")
    return "\n".join(parts)


def visible_length(html_text: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html.unescape(html_text)))


def fit_rich_html(story: dict) -> str:
    rendered = render_rich_html(story)
    if visible_length(rendered) <= MAX_RICH_CHARACTERS:
        return rendered
    compact = dict(story)
    if story.get("events"):
        compact["events"] = list(story.get("events", []))[:24]
    compact["body"] = clamp(story.get("body"), 1400)
    compact["dek"] = clamp(story.get("dek"), 450)
    compact["key_points"] = list(story.get("key_points", []))[:4]
    return render_rich_html(compact)


def plain_caption(rich_html: str) -> str:
    caption = re.sub(r"<br\s*/?>", "\n", rich_html, flags=re.I)
    caption = re.sub(r"</(?:p|h1|h2|h3|footer|aside|blockquote|li|ul)>", "\n", caption, flags=re.I)
    caption = re.sub(r"<[^>]+>", "", caption)
    caption = html.unescape(caption)
    caption = re.sub(r"\n{3,}", "\n\n", caption).strip()
    if len(caption) > MAX_CAPTION_CHARACTERS:
        caption = clamp(caption, MAX_CAPTION_CHARACTERS - 1).rstrip("…") + "…"
    return caption


def _upload_photo(image_path: str, rich_html: str) -> dict:
    caption = plain_caption(rich_html)
    with Path(image_path).open("rb") as photo:
        return telegram_call("sendPhoto", data={"chat_id": TELEGRAM_CHANNEL, "caption": caption}, files={"photo": photo})


def send_story(image_path: str, rich_html: str) -> dict:
    """Reliable two-step rich-photo publishing.

    1) sendPhoto uploads the local image, which is universally supported.
    2) editMessageCaption upgrades the caption to Telegram Rich Message HTML.
       Bot API 10.1+ supports rich_message on editMessageCaption.
    If the upgrade fails, the already-published photo keeps its plain fallback caption.
    """
    sent = _upload_photo(image_path, rich_html)
    if not sent.get("ok"):
        return sent
    result = sent.get("result", {})
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not message_id:
        return sent

    rich_message = {"html": rich_html, "skip_entity_detection": False}
    edited = telegram_call(
        "editMessageCaption",
        data={
            "chat_id": TELEGRAM_CHANNEL,
            "message_id": str(message_id),
            "rich_message": json.dumps(rich_message, ensure_ascii=False),
        },
    )
    if edited.get("ok"):
        return edited
    return sent
