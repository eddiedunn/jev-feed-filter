# /// script
# requires-python = ">=3.11"
# dependencies = ["typesafe-sdk>=0.7.1", "feedparser>=6", "httpx>=0.27"]
# ///
"""Pull news and YouTube feeds, filter them with plain code and Jev, write out/*.html."""

import asyncio
import html
import os
import re
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import httpx
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

ROOT = Path(__file__).parent
NEWS_FORMATS = {
    "straight_news": "Factual reporting of events",
    "analysis": "Explainer or analysis of events",
    "opinion": "Opinion, column, editorial, or commentary",
    "promotional": "Advertising, sponsored content, deals, or product promotion",
}
SLANT_LEVELS = [
    "Neutral, even-handed wording",
    "Leans to one side in word choice or framing",
    "Strongly one-sided: loaded language, partisan framing, or advocacy",
]
VIDEO_FORMATS = {
    "tutorial": "Teaches how to do something, step by step",
    "review": "Reviews, tests, or compares a product",
    "explainer": "Explains a topic, idea, or event; documentary style",
    "news_commentary": "Discusses current news or gives opinions on it",
    "interview_podcast": "Conversation, interview, or podcast episode",
    "reaction": "Reacts to other videos or media",
    "vlog": "Personal vlog, travel, or day-in-the-life",
    "entertainment": "Comedy, challenges, stunts, or experiments for fun",
    "music": "Music performance or music video",
    "gaming": "Video game play or gaming content",
}


@dataclass
class Item:
    title: str
    summary: str
    link: str
    outlet: str
    domain: str
    published: float
    label: str = ""
    hidden_because: list[str] = field(default_factory=list)


def secret(env_var: str, section: dict) -> str:
    """Read a key from the environment, or from gopass if the config names an entry."""
    if value := os.environ.get(env_var):
        return value
    if entry := section.get("gopass_key"):
        return subprocess.run(["gopass", "show", "-o", entry], capture_output=True, text=True, check=True).stdout.strip()
    raise SystemExit(f"Set {env_var}, or set gopass_key in config.toml.")


def domain_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host.removeprefix("www.")


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def entry_time(e) -> float:
    when = e.get("published_parsed") or e.get("updated_parsed")
    return time.mktime(when) if when else 0


def keyword_filter(items: list[Item], keywords: list[str]) -> None:
    for it in items:
        text = f"{it.title} {it.summary}".lower()
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw.lower())}\b", text):
                it.hidden_because.append(f"keyword: {kw}")


def topic_questions(topics: list[str], noun: str) -> dict:
    return {f"topic_{i}": Noul(instructions=f"This {noun} is mainly about {t}.") for i, t in enumerate(topics)}


def apply_topics(it: Item, answers, topics: list[str], threshold: float) -> None:
    for i, topic in enumerate(topics):
        p = answers[f"topic_{i}"].noul
        if p >= threshold:
            it.hidden_because.append(f"topic: {topic} ({p:.2f})")


async def ask_jev(items: list[Item], state_of, qs: dict, jev: dict) -> list:
    """One Jev request per item, all with the same questions. Returns answers in order."""
    key = secret(jev.get("api_key_env", "OPENROUTER_API_KEY"), jev)
    gate = asyncio.Semaphore(jev["concurrency"])
    async with AsyncTypeSafeClient(api_key=key, base_url=jev["base_url"], model=jev["model"]) as client:

        async def one(it: Item):
            async with gate:
                return (await client.system_one(state_of(it), qs)).answers

        return await asyncio.gather(*(one(it) for it in items))


# ---------- news ----------


def read_news_feed(url: str) -> list[Item]:
    parsed = feedparser.parse(url, agent="Mozilla/5.0 jev-feed-filter")
    feed_title = parsed.feed.get("title", domain_of(url))
    items = []
    for e in parsed.entries:
        title, link = e.get("title", ""), e.get("link", "")
        summary = strip_html(e.get("summary", ""))[:500]
        outlet, domain = feed_title, domain_of(link)
        # Google News links are redirects; the real publisher is in <source>.
        if "source" in e and e.source.get("href"):
            outlet, domain = e.source.get("title", outlet), domain_of(e.source["href"])
            title = title.removesuffix(f" - {outlet}")
            summary = ""  # just a list of links
        items.append(Item(title, summary, link, outlet, domain, entry_time(e)))
    return items


def run_news(news: dict, jev: dict) -> list[Item]:
    with ThreadPoolExecutor(8) as pool:
        stories = [s for batch in pool.map(read_news_feed, news["feeds"]) for s in batch]
    seen, unique = set(), []
    for s in stories:
        k = re.sub(r"\W+", "", s.title.lower())
        if k and k not in seen:
            seen.add(k)
            unique.append(s)

    paywall = news["paywall_domains"]
    for s in unique:
        if any(s.domain == d or s.domain.endswith("." + d) for d in paywall):
            s.hidden_because.append(f"paywall ({s.domain})")
    keyword_filter(unique, news["hide_keywords"])

    # Only pay Jev for stories the free checks didn't already drop.
    todo = [s for s in unique if not s.hidden_because]
    qs = {
        "format": Choice(instructions="What kind of piece is this story?", criteria=NEWS_FORMATS),
        "slant": Score(
            instructions="How politically one-sided is the wording and framing of this story?",
            criteria=SLANT_LEVELS,
        ),
        **topic_questions(news["hide_topics"], "story"),
    }

    def state_of(s: Item) -> dict:
        return {"headline": s.title, "outlet": s.outlet} | ({"summary": s.summary} if s.summary else {})

    for s, a in zip(todo, asyncio.run(ask_jev(todo, state_of, qs, jev))):
        fmt = a["format"].choice
        s.label = fmt.replace("_", " ")
        if fmt in news["hide_formats"]:
            s.hidden_because.append(f"format: {fmt}")
        strong = a["slant"].probabilities[len(SLANT_LEVELS) - 1]
        if strong >= jev["strong_slant"]:
            s.hidden_because.append(f"strong slant ({strong:.2f})")
        apply_topics(s, a, news["hide_topics"], jev["topic_match"])
    return unique


# ---------- youtube ----------


def iso_seconds(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d)
    days, h, mins, s = (int(x or 0) for x in m.groups()) if m else (0, 0, 0, 0)
    return days * 86400 + h * 3600 + mins * 60 + s


def clock(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02}:{rest % 60:02}" if h else f"{rest // 60}:{rest % 60:02}"


def subscriptions(api: httpx.Client, handle: str) -> list[str]:
    channel = api.get("channels", params={"part": "id", "forHandle": handle}).json()["items"][0]["id"]
    ids, page = [], None
    while True:
        r = api.get(
            "subscriptions",
            params={"part": "snippet", "channelId": channel, "maxResults": 50, "pageToken": page},
        ).json()
        ids += [i["snippet"]["resourceId"]["channelId"] for i in r["items"]]
        if not (page := r.get("nextPageToken")):
            return ids


def read_channel_feed(channel_id: str) -> list[Item]:
    parsed = feedparser.parse(f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}")
    return [
        Item(
            e.get("title", ""),
            e.get("summary", "")[:500],
            e.get("link", ""),
            parsed.feed.get("title", ""),
            e.get("yt_videoid", ""),  # the video id rides in `domain` for videos
            entry_time(e),
        )
        for e in parsed.entries
    ]


def is_short(video_id: str) -> bool:
    # youtube.com/shorts/<id> loads for a Short and redirects for anything else.
    r = httpx.head(f"https://www.youtube.com/shorts/{video_id}", follow_redirects=False, timeout=10)
    return r.status_code == 200


def run_youtube(yt: dict, jev: dict) -> list[Item]:
    with httpx.Client(base_url="https://www.googleapis.com/youtube/v3/", params={"key": secret("YOUTUBE_API_KEY", yt)}) as api:
        channels = subscriptions(api, yt["channel_handle"])
        with ThreadPoolExecutor(16) as pool:
            feeds = pool.map(read_channel_feed, channels)
        since = time.time() - yt["days"] * 86400
        videos = sorted((v for f in feeds for v in f if v.published >= since), key=lambda v: v.published, reverse=True)

        details = {}
        for i in range(0, len(videos), 50):
            ids = ",".join(v.domain for v in videos[i : i + 50])
            r = api.get("videos", params={"part": "contentDetails,snippet,liveStreamingDetails", "id": ids}).json()
            details |= {d["id"]: d for d in r["items"]}

    maybe_short = []
    for v in videos:
        d = details.get(v.domain)
        if not d:
            v.hidden_because.append("unavailable")
            continue
        v.summary = d["snippet"].get("description", "")[:500]
        # Past streams keep liveStreamingDetails; so do Premieres, which get hidden too.
        if d["snippet"]["liveBroadcastContent"] != "none" or "liveStreamingDetails" in d:
            v.hidden_because.append("livestream")
            continue
        seconds = iso_seconds(d["contentDetails"].get("duration", ""))
        v.label = clock(seconds)
        if seconds <= 180:  # Shorts can run up to 3 minutes
            maybe_short.append(v)
    with ThreadPoolExecutor(16) as pool:
        for v, short in zip(maybe_short, pool.map(lambda v: is_short(v.domain), maybe_short)):
            if short:
                v.hidden_because.append("short")
    keyword_filter(videos, yt["hide_keywords"])

    todo = [v for v in videos if not v.hidden_because]
    qs = {
        "format": Choice(instructions="What kind of video is this?", criteria=VIDEO_FORMATS),
        **topic_questions(yt["hide_topics"], "video"),
    }

    def state_of(v: Item) -> dict:
        return {"title": v.title, "channel": v.outlet, "description": v.summary}

    for v, a in zip(todo, asyncio.run(ask_jev(todo, state_of, qs, jev))):
        v.label = f"{v.label} · {a['format'].choice.replace('_', ' ')}"
        apply_topics(v, a, yt["hide_topics"], jev["topic_match"])
    return videos


# ---------- output ----------


def render(heading: str, items: list[Item]) -> str:
    def row(it: Item) -> str:
        why = f'<span class="why">{html.escape("; ".join(it.hidden_because))}</span>' if it.hidden_because else ""
        label = f'<span class="label">{html.escape(it.label)}</span> ' if it.label else ""
        when = time.strftime("%b %d %H:%M", time.localtime(it.published)) if it.published else ""
        return (
            f'<li><a href="{html.escape(it.link)}" target="_blank" rel="noopener">{html.escape(it.title)}</a>'
            f'<div class="meta">{label}{html.escape(it.outlet)} · {when} {why}</div></li>'
        )

    kept = [it for it in items if not it.hidden_because]
    hidden = [it for it in items if it.hidden_because]
    return f"""<!doctype html><meta charset="utf-8"><title>{heading}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--dim:#666;--link:#1a4fb5;--warn:#a44;--chip:#eef1f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181c;--fg:#e6e6e6;--dim:#999;--link:#8ab4f8;--warn:#e88;--chip:#262a31}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif;max-width:760px;margin:0 auto;padding:16px}}
ul{{list-style:none;padding:0}} li{{margin:0 0 14px}} a{{color:var(--link);text-decoration:none}}
.meta{{color:var(--dim);font-size:13px}} .why{{color:var(--warn)}} summary{{cursor:pointer;color:var(--dim)}}
.label{{background:var(--chip);color:var(--fg);border-radius:4px;padding:0 5px}}
</style>
<h1>{heading} <small style="color:var(--dim);font-size:14px">{len(kept)} shown · {len(hidden)} hidden</small></h1>
<ul>{"".join(map(row, kept))}</ul>
<details><summary>Hidden ({len(hidden)})</summary><ul>{"".join(map(row, hidden))}</ul></details>
"""


def write(name: str, heading: str, items: list[Item]) -> None:
    items.sort(key=lambda it: it.published, reverse=True)
    out = ROOT / "out" / f"{name}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(heading, items))
    shown = sum(not it.hidden_because for it in items)
    print(f"{heading}: {len(items)} items, {shown} shown, {len(items) - shown} hidden -> {out}")


def main() -> None:
    path = ROOT / "config.toml"
    if not path.exists():
        raise SystemExit("No config.toml. Copy config.example.toml to config.toml and edit it.")
    cfg = tomllib.loads(path.read_text())
    write("news", "News", run_news(cfg["news"], cfg["jev"]))
    if cfg.get("youtube", {}).get("channel_handle"):
        write("youtube", "YouTube", run_youtube(cfg["youtube"], cfg["jev"]))


if __name__ == "__main__":
    main()
