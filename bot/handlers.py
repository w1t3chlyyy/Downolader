import asyncio
import logging
import re
import shutil
import tempfile
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from . import config, db, tg
from .downloader import Media, detect, fetch_media

log = logging.getLogger("bot")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
LINE = "┄┄┄┄┄┄┄┄┄┄┄┄"

DEFAULT_WELCOME = {
    "text": (
        "<b>👋 Привет, {name}!</b>\n\n"
        "Пришли мне ссылку — и я скачаю видео или музыку прямо сюда.\n\n"
        "🎬 TikTok · 📸 Instagram · 📌 Pinterest\n"
        "☁️ SoundCloud · 🎧 Яндекс Музыка · ▶️ YouTube"
    ),
    "photo": "",
    "effect": "",
    "sticker": "",
    "buttons": [],
}
DEFAULT_SUBS = (
    "🔒 <b>Подпишись, чтобы продолжить</b>\n\n"
    "Для скачивания нужно быть подписанным на каналы ниже.\n"
    "После подписки нажми «Я подписался» — и я сразу скачаю твою ссылку."
)


# ───────────────────────── Приветствие ─────────────────────────

async def get_welcome() -> dict:
    return {**DEFAULT_WELCOME, **(await db.get_setting("welcome", {}))}


def buttons_markup(buttons: list[dict]) -> dict | None:
    rows = [[{"text": b["text"], "url": b["url"]}] for b in buttons if b.get("text") and b.get("url")]
    return {"inline_keyboard": rows} if rows else None


async def send_welcome(chat_id: int, name: str | None, w: dict | None = None) -> None:
    w = {**DEFAULT_WELCOME, **(w or await get_welcome())}
    text = w["text"].replace("{name}", escape(name or "друг"))
    kb = buttons_markup(w.get("buttons", []))
    effect = w.get("effect") or None

    if w.get("sticker"):
        try:
            await tg.call("sendSticker", chat_id=chat_id, sticker=w["sticker"])
        except tg.TGError as e:
            log.warning("sticker: %s", e)

    async def attempt(eff):
        if w.get("photo"):
            try:
                if len(text) <= 1000:
                    return await tg.send(
                        "sendPhoto", chat_id=chat_id, photo=w["photo"], caption=text,
                        parse_mode="HTML", reply_markup=kb, message_effect_id=eff,
                    )
                await tg.call("sendPhoto", chat_id=chat_id, photo=w["photo"])
            except tg.TGError as e:
                log.warning("welcome photo: %s", e)
        return await tg.send(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb,
            message_effect_id=eff, link_preview_options={"is_disabled": True},
        )

    try:
        await attempt(effect)
    except tg.TGError:
        if not effect:
            raise
        await attempt(None)  # эффекты работают только в личных чатах


# ───────────────────────── Подписки ─────────────────────────

async def is_member(chat_id: int, user_id: int) -> bool:
    try:
        m = await tg.call("getChatMember", chat_id=chat_id, user_id=user_id)
    except tg.TGError as e:
        log.warning("getChatMember %s: %s", chat_id, e)
        return True  # если бот не админ канала — не блокируем пользователей
    return m["status"] in ("creator", "administrator", "member") or (
        m["status"] == "restricted" and m.get("is_member")
    )


async def missing_channels(user_id: int) -> list[dict]:
    chans = await db.select("channels", enabled="eq.true", order="id.asc")
    if not chans:
        return []
    res = await asyncio.gather(*(is_member(c["chat_id"], user_id) for c in chans))
    return [c for c, ok in zip(chans, res) if not ok]


async def send_gate(chat_id: int, user_id: int, missing: list[dict], urls: list[str]) -> None:
    text = await db.get_setting("subs_text", DEFAULT_SUBS)
    rows = [[{"text": f"📢 {c['title']}", "url": c["link"]}] for c in missing if c.get("link")]
    rows.append([{"text": "✅ Я подписался", "callback_data": "chk"}])
    await db.update("users", {"tg_id": user_id}, {"pending_url": "\n".join(urls)})
    await tg.send("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                  reply_markup={"inline_keyboard": rows})


# ───────────────────────── Скачивание ─────────────────────────

def status_text(p, text: str) -> str:
    return f"{p.emoji} <b>{p.name}</b>\n{LINE}\n{text}"


def friendly_error(exc: Exception, p) -> str:
    s = str(exc).lower()
    if isinstance(exc, PermissionError) or "ym_no_token" in s:
        return "🔑 Для Яндекс Музыки не настроен токен. Напиши администратору бота."
    if "ym_not_track" in s:
        return "🎧 Я умею скачивать только <b>отдельные треки</b> Яндекс Музыки, не альбомы и плейлисты."
    if "login" in s or "cookie" in s or "private" in s or "sign in" in s or "rate-limit" in s or "bot" in s:
        return "🔒 Пост приватный или площадка просит вход в аккаунт. Попробуй другую ссылку."
    if "unsupported url" in s:
        return "🤷 Эту ссылку я пока не умею скачивать."
    if "404" in s or "not found" in s or "unavailable" in s or "removed" in s:
        return "🕳 Контент недоступен — возможно, его удалили."
    return "😔 Не получилось скачать. Проверь ссылку и попробуй ещё раз чуть позже."


async def send_media(chat_id: int, m: Media, caption: str, kb: dict, reply_to: int | None) -> None:
    ext = m.path.suffix.lower()
    method, field = {"video": ("sendVideo", "video"), "audio": ("sendAudio", "audio"),
                     "photo": ("sendPhoto", "photo")}[m.kind]
    if m.kind == "audio" and ext not in (".mp3", ".m4a"):
        method, field = "sendDocument", "document"
    data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML", "reply_markup": kb}
    if reply_to:
        data["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if method == "sendVideo":
        data.update(supports_streaming=True, duration=m.duration, width=m.width, height=m.height)
    if method == "sendAudio":
        data.update(title=m.title or None, performer=m.author or None, duration=m.duration)
    files = {field: (m.path.name, open(m.path, "rb"))}
    if method == "sendAudio" and m.thumb and m.thumb.exists():
        data["thumbnail"] = "attach://thumb"
        files["thumb"] = (m.thumb.name, open(m.thumb, "rb"))
    try:
        await tg.upload(method, data, files)
    finally:
        for _, f in files.values():
            f.close()


async def process(chat_id: int, uid: int, url: str, reply_to: int | None = None) -> None:
    p = detect(url)
    rp = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
    st = await tg.call("sendMessage", chat_id=chat_id, parse_mode="HTML", reply_parameters=rp,
                       text=status_text(p, "🔎 Анализирую ссылку…"))
    mid = st["message_id"]

    async def step(t: str) -> None:
        try:
            await tg.call("editMessageText", chat_id=chat_id, message_id=mid, parse_mode="HTML",
                          text=status_text(p, t))
        except tg.TGError:
            pass

    tmp = Path(tempfile.mkdtemp(prefix="dl_", dir="/tmp"))
    ok, kind = False, None
    try:
        await step("⬇️ Скачиваю…")
        media = await fetch_media(url, p, tmp)
        kind = media.kind
        if media.path.stat().st_size > config.MAX_MB * 1024 * 1024:
            await step(f"📦 Файл больше <b>{config.MAX_MB} МБ</b> — это лимит Telegram для ботов.")
            return
        await step("📤 Отправляю…")
        bot = await tg.me()
        parts = []
        if media.title:
            parts.append(f"<b>{escape(media.title[:180])}</b>")
        if media.author:
            parts.append(f"👤 {escape(media.author[:80])}")
        if media.note:
            parts.append(media.note)
        parts.append(f"{p.emoji} {p.name} · 📥 @{bot.get('username', 'bot')}")
        kb = {"inline_keyboard": [[{"text": "🔗 Оригинал", "url": url}]]}
        await send_media(chat_id, media, "\n".join(parts), kb, reply_to)
        ok = True
        await tg.call("deleteMessage", chat_id=chat_id, message_id=mid)
    except Exception as exc:  # noqa: BLE001
        log.exception("process %s", url)
        await step(friendly_error(exc, p))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            await db.insert("downloads", {"tg_id": uid, "platform": p.key, "kind": kind, "ok": ok})
        except Exception:  # noqa: BLE001
            pass


async def run_links(chat_id: int, uid: int, urls: list[str], reply_to: int | None) -> None:
    await asyncio.gather(*(process(chat_id, uid, u, reply_to) for u in urls[:2]))


# ───────────────────────── Апдейты ─────────────────────────

async def admin_helpers(m: dict, chat_id: int) -> bool:
    """Подсказки для админов: ID стикеров, кастом-эмодзи и каналов."""
    text = m.get("text") or ""
    if text.startswith("/admin"):
        base = config.public_url()
        kb = {"inline_keyboard": [[{"text": "Открыть панель", "url": base + "/admin"}]]} if base else None
        await tg.send("sendMessage", chat_id=chat_id, text="⚙️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb)
        return True
    if st := m.get("sticker"):
        t = f"<b>file_id стикера:</b>\n<code>{st['file_id']}</code>"
        if st.get("custom_emoji_id"):
            t += f"\n\n<b>ID кастом-эмодзи:</b>\n<code>{st['custom_emoji_id']}</code>"
        await tg.send("sendMessage", chat_id=chat_id, text=t, parse_mode="HTML")
        return True
    ids = [e["custom_emoji_id"] for e in (m.get("entities") or []) if e.get("type") == "custom_emoji"]
    if ids:
        t = "<b>ID кастом-эмодзи:</b>\n" + "\n".join(f"<code>{i}</code>" for i in ids)
        await tg.send("sendMessage", chat_id=chat_id, text=t, parse_mode="HTML")
        return True
    fo = m.get("forward_origin") or {}
    if fo.get("type") == "channel":
        ch = fo["chat"]
        await tg.send("sendMessage", chat_id=chat_id, parse_mode="HTML",
                      text=f"<b>{escape(ch.get('title', ''))}</b>\nID канала: <code>{ch['id']}</code>")
        return True
    return False


async def on_message(m: dict) -> None:
    chat, u = m["chat"], m.get("from") or {}
    chat_id, uid = chat["id"], u.get("id")
    if not uid or u.get("is_bot"):
        return
    private = chat["type"] == "private"
    text = m.get("text") or m.get("caption") or ""

    if private:
        await db.insert("users", {"tg_id": uid, "first_name": u.get("first_name"), "username": u.get("username"),
                                  "last_seen": datetime.now(timezone.utc).isoformat()},
                        on_conflict="tg_id", merge=True)
        if uid in config.ADMIN_IDS and await admin_helpers(m, chat_id):
            return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
    if cmd == "/start":
        return await send_welcome(chat_id, u.get("first_name"))
    if cmd == "/help":
        return await send_welcome(chat_id, u.get("first_name"))

    urls = list(dict.fromkeys(URL_RE.findall(text)))[:2]
    if not urls:
        if private:
            await tg.call("sendMessage", chat_id=chat_id, text="🔗 Пришли ссылку на видео, трек или пост — и я всё скачаю.")
        return
    missing = await missing_channels(uid)
    if missing:
        return await send_gate(chat_id, uid, missing, urls)
    await run_links(chat_id, uid, urls, m["message_id"])


async def on_callback(cb: dict) -> None:
    uid = cb["from"]["id"]
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if cb.get("data") != "chk" or not chat_id:
        return await tg.call("answerCallbackQuery", callback_query_id=cb["id"])
    if await missing_channels(uid):
        return await tg.call("answerCallbackQuery", callback_query_id=cb["id"], show_alert=True,
                             text="Ты ещё не подписался на все каналы 🙈")
    await tg.call("answerCallbackQuery", callback_query_id=cb["id"], text="Отлично! Скачиваю… 🚀")
    try:
        await tg.call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])
    except tg.TGError:
        pass
    rows = await db.select("users", tg_id=f"eq.{uid}")
    urls = [x for x in ((rows[0].get("pending_url") or "") if rows else "").split("\n") if x]
    if not urls:
        return await tg.call("sendMessage", chat_id=chat_id, text="✅ Готово! Теперь пришли ссылку.")
    await db.update("users", {"tg_id": uid}, {"pending_url": None})
    await run_links(chat_id, uid, urls, None)


async def handle_update(u: dict) -> None:
    try:  # защита от повторной доставки вебхука
        if not await db.insert("updates", {"update_id": u["update_id"]}, on_conflict="update_id", ignore=True):
            return
    except Exception:  # noqa: BLE001
        log.exception("dedupe")
    if "message" in u:
        await on_message(u["message"])
    elif "callback_query" in u:
        await on_callback(u["callback_query"])
