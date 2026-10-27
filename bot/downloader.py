import asyncio
import base64
import json
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path

import httpx
import yt_dlp

from . import config

YM_RE = re.compile(r"music\.yandex\.\w+/(?:album/(\d+)/)?track/(\d+)")
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".opus", ".webm"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class Platform:
    key: str
    name: str
    emoji: str


PLATFORMS = [
    ("tiktok.com", Platform("tiktok", "TikTok", "🎬")),
    ("instagram.com", Platform("instagram", "Instagram", "📸")),
    ("pinterest.", Platform("pinterest", "Pinterest", "📌")),
    ("pin.it", Platform("pinterest", "Pinterest", "📌")),
    ("soundcloud.com", Platform("soundcloud", "SoundCloud", "☁️")),
    ("music.yandex.", Platform("yandex", "Яндекс Музыка", "🎧")),
    ("youtube.com", Platform("youtube", "YouTube", "▶️")),
    ("youtu.be", Platform("youtube", "YouTube", "▶️")),
    ("twitter.com", Platform("twitter", "X / Twitter", "🐦")),
    ("x.com", Platform("twitter", "X / Twitter", "🐦")),
    ("vk.com", Platform("vk", "VK", "💬")),
    ("vkvideo.ru", Platform("vk", "VK Видео", "💬")),
    ("bandcamp.com", Platform("bandcamp", "Bandcamp", "🎸")),
    ("facebook.com", Platform("facebook", "Facebook", "📘")),
    ("reddit.com", Platform("reddit", "Reddit", "👽")),
    ("twitch.tv", Platform("twitch", "Twitch", "🟣")),
]
AUDIO_FIRST = {"soundcloud", "bandcamp"}


def detect(url: str) -> Platform:
    low = url.lower()
    for needle, plat in PLATFORMS:
        if needle in low:
            return plat
    return Platform("other", "Ссылка", "🔗")


@dataclass
class Media:
    path: Path
    kind: str  # video | audio | photo
    title: str = ""
    author: str = ""
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    thumb: Path | None = None
    note: str = ""
    source: str = ""


def _pick(out: Path, exts: set[str]) -> Path | None:
    files = [p for p in out.iterdir() if p.suffix.lower() in exts]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def _cookies() -> str | None:
    if not config.COOKIES_B64:
        return None
    p = Path("/tmp/cookies.txt")
    if not p.exists():
        p.write_bytes(base64.b64decode(config.COOKIES_B64))
    return str(p)


def _ydl(url: str, out: Path, audio: bool) -> Media:
    limit = f"{config.MAX_MB - 2}M"
    opts = {
        "outtmpl": str(out / "%(id).80s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "cachedir": False,
        "http_headers": {"User-Agent": config.UA},
    }
    if c := _cookies():
        opts["cookiefile"] = c
    # На Vercel нет ffmpeg → берём только готовые одиночные файлы без склейки
    if audio:
        opts["format"] = "bestaudio[ext=mp3][protocol^=http]/bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio"
    else:
        opts["format"] = f"b[ext=mp4][filesize<{limit}]?/b[ext=mp4]/b"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info.get("entries"):
        info = next(e for e in list(info["entries"]) if e)

    meta = dict(
        title=info.get("track") or info.get("title") or "",
        author=info.get("artist") or info.get("uploader") or info.get("channel") or "",
        duration=int(info["duration"]) if info.get("duration") else None,
        source=info.get("webpage_url") or url,
    )
    if audio:
        if f := _pick(out, AUDIO_EXT):
            return Media(f, "audio", **meta)
    if f := _pick(out, VIDEO_EXT):
        return Media(f, "video", width=info.get("width"), height=info.get("height"), **meta)
    if f := _pick(out, AUDIO_EXT):
        return Media(f, "audio", **meta)
    if f := _pick(out, IMG_EXT):
        return Media(f, "photo", **meta)
    raise RuntimeError("Файл не найден после загрузки")


_ym = None


def _yandex(url: str, out: Path) -> Media:
    global _ym
    from yandex_music import Client

    m = YM_RE.search(url)
    if not m:
        raise ValueError("ym_not_track")
    album_id, track_id = m.groups()
    if _ym is None:
        kw = {}
        if config.YANDEX_PROXY:
            from yandex_music.utils.request import Request

            kw["request"] = Request(proxy_url=config.YANDEX_PROXY)
        _ym = Client(config.YANDEX_TOKEN or None, **kw).init()
    track = _ym.tracks([f"{track_id}:{album_id}" if album_id else track_id])[0]
    path = out / f"{track.id}.mp3"
    try:
        track.download(str(path), codec="mp3", bitrate_in_kbps=320)
    except Exception:
        track.download(str(path), codec="mp3", bitrate_in_kbps=192)
    thumb: Path | None = out / f"{track.id}.jpg"
    try:
        track.download_cover(str(thumb), size="400x400")
    except Exception:
        thumb = None
    return Media(
        path, "audio", title=track.title or "", author=", ".join(track.artists_name()),
        duration=int(track.duration_ms / 1000) if track.duration_ms else None, thumb=thumb,
        note="" if config.YANDEX_TOKEN else "⏱ Без подписки Яндекс отдаёт только первые 30 секунд",
    )


async def _og(url: str, out: Path) -> Media:
    """Pinterest: пробуем публичный API пина, затем og-теги страницы."""
    hdr = {"User-Agent": config.UA, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(headers=hdr, timeout=25, follow_redirects=True) as s:
        r = await s.get(url)
        html = r.text
        m = re.search(r"/pin/(\d+)", str(r.url)) or re.search(r"/pin/(\d+)", html)
        title, cands = "", []
        if m:
            pid = m.group(1)
            opts = json.dumps({"options": {"id": pid, "field_set_key": "detailed"}, "context": {}}, separators=(",", ":"))
            try:
                api = await s.get(
                    "https://www.pinterest.com/resource/PinResource/get/",
                    params={"source_url": f"/pin/{pid}/", "data": opts},
                    headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
                )
                data = api.json()["resource_response"]["data"]
                title = data.get("title") or data.get("grid_title") or ""
                vids = [v for v in ((data.get("videos") or {}).get("video_list") or {}).values()
                        if str(v.get("url", "")).endswith(".mp4")]
                if vids:
                    cands.append(("video", max(vids, key=lambda v: v.get("width", 0))["url"]))
                orig = ((data.get("images") or {}).get("orig") or {}).get("url")
                if orig:
                    cands.append(("photo", orig))
            except Exception:  # noqa: BLE001
                pass
        for prop, kind in (("og:video", "video"), ("og:image", "photo")):
            for pat in (
                rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{prop}["\']',
            ):
                mm = re.search(pat, html)
                if mm:
                    src = unescape(mm.group(1))
                    if kind == "photo" or src.endswith(".mp4"):
                        cands.append((kind, src))
                    break
        for kind, src in cands:
            tries = [re.sub(r"/\d+x/", "/originals/", src), src] if kind == "photo" else [src]
            for c in tries:
                rr = await s.get(c)
                if rr.status_code == 200 and rr.content:
                    path = out / ("pin.mp4" if kind == "video" else "pin.jpg")
                    path.write_bytes(rr.content)
                    return Media(path, kind, title=title)
    raise RuntimeError("Медиа не найдено на странице")


async def fetch_media(url: str, p: Platform, out: Path) -> Media:
    if p.key == "yandex":
        return await asyncio.to_thread(_yandex, url, out)
    try:
        return await asyncio.to_thread(_ydl, url, out, p.key in AUDIO_FIRST)
    except Exception:
        if p.key == "pinterest":
            return await _og(url, out)
        raise


def _sc_search(query: str, out: Path) -> Media:
    """Ищет трек на SoundCloud и качает первый результат, который не защищён DRM."""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": True, "cachedir": False}) as ydl:
        res = ydl.extract_info(f"scsearch5:{query}", download=False)
    last: Exception | None = None
    for e in (res.get("entries") or [])[:4]:
        u = e.get("url") or e.get("webpage_url")
        if not u:
            continue
        try:
            return _ydl(u, out, True)
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise last or RuntimeError("Ничего не найдено")


async def search_media(query: str, out: Path) -> Media:
    return await asyncio.to_thread(_sc_search, query, out)
