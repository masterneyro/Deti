"""
M7 — Reach watcher (YouTube + Instagram).

Что делает:
  1. YouTube (Data API v3, ключ YOUTUBE_API_KEY):
     - наш канал (YOUTUBE_CHANNEL_ID): подписчики, просмотры, свежие ролики;
     - каналы конкурентов из data/reach/sources.json: то же самое;
     - поиск по нишевым запросам: самые просматриваемые ролики за неделю.
  2. Instagram (Graph API, бизнес/автор-аккаунт, INSTAGRAM_USER_ID + INSTAGRAM_ACCESS_TOKEN):
     - наш аккаунт: подписчики, охват за неделю, свежие публикации;
     - аккаунты конкурентов через business_discovery (только бизнес/автор-аккаунты);
     - топ-публикации по хэштегам ниши.
  3. Сравнивает со снимком прошлого запуска → прирост подписчиков и просмотров.
  4. Через Claude (если есть ANTHROPIC_API_KEY): что сейчас «заходит» в нише и
     идеи для роликов/постов/статей.
  5. Сохраняет снимок в data/reach/<дата>.json и шлёт отчёт в Telegram.

Каждый источник независим: нет ключа или API вернул ошибку — блок пропускается,
остальной отчёт уходит.

Запуск:
    python3 orchestrator.py m7
    python3 orchestrator.py m7 --dry-run   # без Telegram
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
from pathlib import Path

import requests

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(THIS_DIR.parent))

from notifiers.humanize import plural  # noqa: E402

log = logging.getLogger(__name__)

DATA_DIR = THIS_DIR.parent / "data" / "reach"
SOURCES_JSON = DATA_DIR / "sources.json"

YT_API = "https://www.googleapis.com/youtube/v3"
IG_API = "https://graph.facebook.com/v21.0"
TIMEOUT = 20
LOOKBACK_DAYS = 7
RECENT_PER_CHANNEL = 10
TOP_PER_QUERY = 5


# ─────────────────────────── helpers ───────────────────────────

def load_sources() -> dict:
    if not SOURCES_JSON.exists():
        return {}
    try:
        return json.loads(SOURCES_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.warning("sources.json битый (%s) — конкуренты и запросы пропущены", e)
        return {}


def _parse_ts(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00").replace("+0000", "+00:00"))
    except ValueError:
        return None


def _age_days(published: str) -> float:
    ts = _parse_ts(published)
    if not ts:
        return 0.0
    return max((dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 86400, 0.1)


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fmt_num(n: int) -> str:
    """12345 → '12,3 тыс.', 1500000 → '1,5 млн'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".replace(".", ",") + " млн"
    if n >= 10_000:
        return f"{n / 1000:.1f}".replace(".", ",") + " тыс."
    return f"{n:,}".replace(",", " ")


def fmt_delta(cur: int, prev: int | None) -> str:
    if prev is None:
        return ""
    d = cur - prev
    if d == 0:
        return " (без изменений)"
    return f" ({'+' if d > 0 else '−'}{fmt_num(abs(d))} за неделю)"


# ─────────────────────────── YouTube ───────────────────────────

def _yt_get(endpoint: str, **params) -> dict:
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    r = requests.get(f"{YT_API}/{endpoint}", params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _yt_videos(video_ids: list[str]) -> list[dict]:
    if not video_ids:
        return []
    data = _yt_get("videos", part="snippet,statistics", id=",".join(video_ids[:50]))
    out = []
    for v in data.get("items", []):
        st = v.get("statistics", {})
        sn = v.get("snippet", {})
        views = _int(st.get("viewCount"))
        out.append({
            "id": v["id"],
            "title": sn.get("title", "")[:150],
            "channel": sn.get("channelTitle", ""),
            "published": sn.get("publishedAt", ""),
            "views": views,
            "likes": _int(st.get("likeCount")),
            "comments": _int(st.get("commentCount")),
            "views_per_day": round(views / _age_days(sn.get("publishedAt", "")), 1),
            "url": f"https://youtu.be/{v['id']}",
        })
    return out


def yt_channel(name: str, channel_id: str = "", handle: str = "") -> dict:
    """Статистика канала + последние ролики. channel_id ('UC…') или handle ('@name')."""
    params = {"part": "snippet,statistics,contentDetails"}
    if channel_id:
        params["id"] = channel_id
    else:
        params["forHandle"] = handle if handle.startswith("@") else f"@{handle}"
    items = _yt_get("channels", **params).get("items", [])
    if not items:
        return {"name": name, "error": "канал не найден"}
    ch = items[0]
    st = ch.get("statistics", {})
    uploads = ch.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    videos: list[dict] = []
    if uploads:
        pl = _yt_get("playlistItems", part="contentDetails", playlistId=uploads,
                     maxResults=RECENT_PER_CHANNEL)
        ids = [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        videos = _yt_videos(ids)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)
    new_videos = [v for v in videos if (_parse_ts(v["published"]) or cutoff) > cutoff]
    return {
        "name": name or ch.get("snippet", {}).get("title", ""),
        "id": ch["id"],
        "subscribers": _int(st.get("subscriberCount")),
        "views": _int(st.get("viewCount")),
        "video_count": _int(st.get("videoCount")),
        "recent_videos": videos,
        "new_this_week": len(new_videos),
    }


def yt_search_top(query: str) -> list[dict]:
    """Самые просматриваемые ролики по запросу за LOOKBACK_DAYS дней (RU)."""
    after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS))
    data = _yt_get("search", part="id", q=query, type="video", order="viewCount",
                   publishedAfter=after.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   regionCode="RU", relevanceLanguage="ru", maxResults=TOP_PER_QUERY)
    ids = [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
    return sorted(_yt_videos(ids), key=lambda v: v["views"], reverse=True)


def collect_youtube(sources: dict) -> dict:
    if not os.environ.get("YOUTUBE_API_KEY", "").strip():
        log.info("YOUTUBE_API_KEY не задан — YouTube пропущен")
        return {"skipped": "нет YOUTUBE_API_KEY"}

    yt_cfg = sources.get("youtube", {})
    out: dict = {"own": None, "competitors": [], "trends": []}

    own_id = os.environ.get("YOUTUBE_CHANNEL_ID", "").strip()
    if own_id:
        try:
            out["own"] = yt_channel("Наш канал", channel_id=own_id)
        except Exception as e:
            log.warning("YouTube, наш канал: %s", e)
            out["own"] = {"name": "Наш канал", "error": str(e)[:200]}

    for c in yt_cfg.get("channels", []):
        if not (c.get("id") or c.get("handle")):
            continue
        try:
            out["competitors"].append(yt_channel(c.get("name", ""), c.get("id", ""), c.get("handle", "")))
        except Exception as e:
            log.warning("YouTube, %s: %s", c.get("name"), e)
            out["competitors"].append({"name": c.get("name", "?"), "error": str(e)[:200]})

    for q in yt_cfg.get("queries", []):
        try:
            out["trends"].append({"query": q, "videos": yt_search_top(q)})
        except Exception as e:
            log.warning("YouTube, поиск «%s»: %s", q, e)
    return out


# ─────────────────────────── Instagram ───────────────────────────

MEDIA_FIELDS = "caption,media_type,permalink,timestamp,like_count,comments_count"


def _ig_get(path: str, **params) -> dict:
    params["access_token"] = os.environ["INSTAGRAM_ACCESS_TOKEN"]
    r = requests.get(f"{IG_API}/{path}", params=params, timeout=TIMEOUT)
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {msg[:200]}")
    return r.json()


def _ig_media(items: list[dict]) -> list[dict]:
    out = []
    for m in items:
        likes = _int(m.get("like_count"))
        comments = _int(m.get("comments_count"))
        out.append({
            "caption": (m.get("caption") or "").replace("\n", " ")[:150],
            "type": m.get("media_type", ""),
            "url": m.get("permalink", ""),
            "published": m.get("timestamp", ""),
            "likes": likes,
            "comments": comments,
            "engagement": likes + comments,
        })
    return out


def ig_own(user_id: str) -> dict:
    acc = _ig_get(user_id, fields="username,followers_count,media_count")
    media = _ig_get(f"{user_id}/media", fields=MEDIA_FIELDS, limit=RECENT_PER_CHANNEL)
    result = {
        "name": f"@{acc.get('username', '')}",
        "followers": _int(acc.get("followers_count")),
        "media_count": _int(acc.get("media_count")),
        "recent_media": _ig_media(media.get("data", [])),
    }
    # Охват — метрика Insights; набор метрик Meta периодически меняет, поэтому не критично.
    try:
        until = dt.datetime.now(dt.timezone.utc)
        since = until - dt.timedelta(days=LOOKBACK_DAYS)
        ins = _ig_get(f"{user_id}/insights", metric="reach,views", period="day",
                      metric_type="total_value",
                      since=int(since.timestamp()), until=int(until.timestamp()))
        for m in ins.get("data", []):
            result[f"week_{m['name']}"] = _int(m.get("total_value", {}).get("value"))
    except Exception as e:
        log.info("Instagram insights недоступны: %s", e)
    return result


def ig_competitor(user_id: str, username: str) -> dict:
    username = username.lstrip("@")
    fields = (f"business_discovery.username({username})"
              f"{{username,followers_count,media_count,media.limit({RECENT_PER_CHANNEL}){{{MEDIA_FIELDS}}}}}")
    bd = _ig_get(user_id, fields=fields).get("business_discovery", {})
    media = _ig_media(bd.get("media", {}).get("data", []))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)
    return {
        "name": f"@{username}",
        "followers": _int(bd.get("followers_count")),
        "media_count": _int(bd.get("media_count")),
        "recent_media": media,
        "new_this_week": sum(1 for m in media if (_parse_ts(m["published"]) or cutoff) > cutoff),
    }


def ig_hashtag_top(user_id: str, tag: str) -> list[dict]:
    tag = tag.lstrip("#")
    found = _ig_get("ig_hashtag_search", user_id=user_id, q=tag).get("data", [])
    if not found:
        return []
    top = _ig_get(f"{found[0]['id']}/top_media", user_id=user_id,
                  fields="caption,media_type,permalink,timestamp,like_count,comments_count", limit=25)
    media = _ig_media(top.get("data", []))
    return sorted(media, key=lambda m: m["engagement"], reverse=True)[:TOP_PER_QUERY]


def collect_instagram(sources: dict) -> dict:
    user_id = os.environ.get("INSTAGRAM_USER_ID", "").strip()
    if not (user_id and os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()):
        log.info("INSTAGRAM_USER_ID / INSTAGRAM_ACCESS_TOKEN не заданы — Instagram пропущен")
        return {"skipped": "нет INSTAGRAM_USER_ID / INSTAGRAM_ACCESS_TOKEN"}

    ig_cfg = sources.get("instagram", {})
    out: dict = {"own": None, "competitors": [], "trends": []}
    try:
        out["own"] = ig_own(user_id)
    except Exception as e:
        log.warning("Instagram, наш аккаунт: %s", e)
        out["own"] = {"name": "Наш аккаунт", "error": str(e)[:200]}

    for acc in ig_cfg.get("accounts", []):
        try:
            out["competitors"].append(ig_competitor(user_id, acc))
        except Exception as e:
            log.warning("Instagram, %s: %s", acc, e)
            out["competitors"].append({"name": f"@{acc.lstrip('@')}", "error": str(e)[:200]})

    for tag in ig_cfg.get("hashtags", []):
        try:
            out["trends"].append({"hashtag": f"#{tag.lstrip('#')}", "media": ig_hashtag_top(user_id, tag)})
        except Exception as e:
            log.warning("Instagram, #%s: %s", tag, e)
    return out


# ─────────────────────────── snapshot / delta ───────────────────────────

def load_previous(today: str) -> dict:
    snaps = sorted(p for p in DATA_DIR.glob("????-??-??.json") if p.stem < today)
    if not snaps:
        return {}
    try:
        return json.loads(snaps[-1].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _prev_value(prev: dict, platform: str, name: str, key: str) -> int | None:
    block = prev.get(platform) or {}
    for acc in [block.get("own")] + list(block.get("competitors", [])):
        if acc and acc.get("name") == name and key in acc:
            return _int(acc[key])
    return None


# ─────────────────────────── Claude ───────────────────────────

SYSTEM_PROMPT = """Ты — SMM- и контент-стратег. Тебе дают сводку по YouTube и Instagram:
наш канал/аккаунт, конкуренты и самые популярные ролики/посты ниши за неделю.

Ответь по-русски, простым языком для владельца бизнеса, без англицизмов и жаргона.
Формат — обычный текст без markdown (без *, #, таблиц):

Что сейчас заходит:
1–3 пункта: какие темы/форматы набирают просмотры и почему (опирайся на цифры).

Идеи на неделю:
до 5 нумерованных идей. Каждая — одна строка: формат (ролик / Shorts / Reels / пост /
статья в блог) + конкретная тема + чем зацепить.

Не выдумывай данные, которых нет в сводке. Если данных мало — так и скажи и дай 2–3 идеи."""


def _brief(report: dict) -> str:
    lines: list[str] = []
    yt = report.get("youtube", {})
    for acc in ([yt.get("own")] if yt.get("own") else []) + yt.get("competitors", []):
        if acc.get("error"):
            continue
        lines.append(f"YouTube «{acc['name']}»: {acc['subscribers']} подписчиков")
        for v in acc.get("recent_videos", [])[:5]:
            lines.append(f"  - {v['title']} — {v['views']} просм., {v['views_per_day']}/день")
    for t in yt.get("trends", []):
        lines.append(f"YouTube, топ по запросу «{t['query']}»:")
        for v in t["videos"]:
            lines.append(f"  - {v['title']} ({v['channel']}) — {v['views']} просм.")
    ig = report.get("instagram", {})
    for acc in ([ig.get("own")] if ig.get("own") else []) + ig.get("competitors", []):
        if acc.get("error"):
            continue
        lines.append(f"Instagram {acc['name']}: {acc['followers']} подписчиков")
        for m in acc.get("recent_media", [])[:5]:
            lines.append(f"  - [{m['type']}] {m['caption'][:100]} — {m['likes']} лайков, {m['comments']} комм.")
    for t in ig.get("trends", []):
        lines.append(f"Instagram, топ по {t['hashtag']}:")
        for m in t["media"]:
            lines.append(f"  - [{m['type']}] {m['caption'][:100]} — {m['engagement']} реакций")
    return "\n".join(lines)


def claude_ideas(report: dict) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    brief = _brief(report)
    if not api_key or not brief:
        return ""
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK не установлен — идеи пропущены")
        return ""
    try:
        response = anthropic.Anthropic(api_key=api_key).messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"Сводка за неделю:\n\n{brief}"}],
        )
    except Exception as e:
        log.warning("Claude (reach) недоступен: %s", e)
        return ""
    text = response.content[0].text.strip() if response.content else ""
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    return text.strip()


# ─────────────────────────── Telegram ───────────────────────────

def render_telegram(report: dict, prev: dict) -> str:
    lines = [f"📣 Охват в YouTube и Instagram · {report['date']}"]

    yt = report.get("youtube", {})
    if yt.get("skipped"):
        lines.append(f"\n▶️ YouTube: не подключён ({yt['skipped']})")
    else:
        lines.append("\n▶️ YouTube")
        for acc in ([yt["own"]] if yt.get("own") else []) + yt.get("competitors", []):
            if acc.get("error"):
                lines.append(f"  {acc['name']}: не удалось проверить ({acc['error']})")
                continue
            subs = acc["subscribers"]
            lines.append(f"  {acc['name']}: {fmt_num(subs)} подписчиков"
                         f"{fmt_delta(subs, _prev_value(prev, 'youtube', acc['name'], 'subscribers'))}")
            n = acc.get("new_this_week", 0)
            lines.append(f"    за неделю: {n} {plural(n, 'новый ролик', 'новых ролика', 'новых роликов')}")
            best = max(acc.get("recent_videos", []), key=lambda v: v["views_per_day"], default=None)
            if best:
                lines.append(f"    быстрее всех растёт: «{best['title'][:70]}» — "
                             f"{fmt_num(int(best['views_per_day']))} просм./день")
        for t in yt.get("trends", []):
            if not t["videos"]:
                continue
            lines.append(f"  🔥 Топ недели по «{t['query']}»:")
            for v in t["videos"][:3]:
                lines.append(f"    • {v['title'][:70]} ({v['channel']}) — {fmt_num(v['views'])} просм.")

    ig = report.get("instagram", {})
    if ig.get("skipped"):
        lines.append(f"\n📸 Instagram: не подключён ({ig['skipped']})")
    else:
        lines.append("\n📸 Instagram")
        for acc in ([ig["own"]] if ig.get("own") else []) + ig.get("competitors", []):
            if acc.get("error"):
                lines.append(f"  {acc['name']}: не удалось проверить ({acc['error']})")
                continue
            fol = acc["followers"]
            lines.append(f"  {acc['name']}: {fmt_num(fol)} подписчиков"
                         f"{fmt_delta(fol, _prev_value(prev, 'instagram', acc['name'], 'followers'))}")
            if "week_reach" in acc:
                lines.append(f"    охват за неделю: {fmt_num(acc['week_reach'])} человек")
            if "new_this_week" in acc:
                n = acc["new_this_week"]
                lines.append(f"    за неделю: {n} {plural(n, 'публикация', 'публикации', 'публикаций')}")
            best = max(acc.get("recent_media", []), key=lambda m: m["engagement"], default=None)
            if best and best["engagement"]:
                lines.append(f"    лучший пост: «{best['caption'][:60] or 'без подписи'}» — "
                             f"{fmt_num(best['engagement'])} реакций")
        for t in ig.get("trends", []):
            if not t["media"]:
                continue
            lines.append(f"  🔥 Топ по {t['hashtag']}:")
            for m in t["media"][:3]:
                lines.append(f"    • {m['caption'][:60] or 'без подписи'} — {fmt_num(m['engagement'])} реакций")

    if report.get("ideas"):
        lines.append("\n💡 " + report["ideas"])
    return "\n".join(lines)


# ─────────────────────────── entry ───────────────────────────

def run_reach(dry_run: bool = False) -> Path:
    today = dt.date.today().isoformat()
    log.info("=== M7 reach watcher · %s ===", today)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    sources = load_sources()
    report: dict = {
        "date": today,
        "youtube": collect_youtube(sources),
        "instagram": collect_instagram(sources),
    }
    report["ideas"] = claude_ideas(report)
    prev = load_previous(today)

    path = DATA_DIR / f"{today}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Снимок: %s", path)

    text = render_telegram(report, prev)
    if dry_run:
        print(text)
    elif os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        from notifiers.telegram import send_telegram
        send_telegram(text)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 reach watcher (YouTube + Instagram)")
    parser.add_argument("--dry-run", action="store_true", help="Не слать в Telegram")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_reach(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
