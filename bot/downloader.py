import asyncio
import base64
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
    if not config.YANDEX_TOKEN:
        raise PermissionError("ym_no_token")
    album_id, track_id = m.groups()
    if _ym is None:
        _ym = Client(config.YANDEX_TOKEN).init()
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
    )


async def _og(url: str, out: Path) -> Media:
    """Запасной путь для Pinterest: og:video / og:image со страницы пина."""
    async with httpx.AsyncClient(headers={"User-Agent": config.UA}, timeout=25, follow_redirects=True) as s:
        html = (await s.get(url)).text
        t = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)', html)
        title = unescape(t.group(1)) if t else ""
        for prop, kind, ext in (("og:video", "video", ".mp4"), ("og:image", "photo", ".jpg")):
            m = re.search(rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']+)', html)
            if not m:
                continue
            src = unescape(m.group(1))
            cands = [re.sub(r"/\d+x/", "/originals/", src), src] if kind == "photo" else [src]
            for c in cands:
                r = await s.get(c)
                if r.status_code == 200:
                    path = out / f"pin{ext}"
                    path.write_bytes(r.content)
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
