import asyncio
import base64
import hashlib
import hmac
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from . import config, db, handlers, tg

router = APIRouter()
HTML = Path(__file__).with_name("admin.html")


# ───────── авторизация (подписанная cookie) ─────────

def _sign(exp: int) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def _valid(tok: str) -> bool:
    try:
        exp, sig = tok.split(".")
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(int(exp)))
    except Exception:  # noqa: BLE001
        return False


def auth(request: Request) -> None:
    if not _valid(request.cookies.get("adm", "")):
        raise HTTPException(401, "Нужен вход")


@router.get("/admin", response_class=HTMLResponse)
async def page() -> str:
    return HTML.read_text(encoding="utf-8")


@router.post("/admin/api/login")
async def login(body: dict, response: Response):
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "Не задан ADMIN_PASSWORD в переменных окружения")
    if not hmac.compare_digest(str(body.get("password", "")), config.ADMIN_PASSWORD):
        await asyncio.sleep(1)
        raise HTTPException(403, "Неверный пароль")
    exp = int(time.time()) + 7 * 86400
    response.set_cookie("adm", f"{exp}.{_sign(exp)}", max_age=7 * 86400, httponly=True, secure=True, samesite="lax")
    return {"ok": True}


@router.post("/admin/api/logout")
async def logout(response: Response):
    response.delete_cookie("adm")
    return {"ok": True}


@router.get("/admin/api/me")
async def me(_=Depends(auth)):
    return {"ok": True}


# ───────── дашборд и вебхук ─────────

def _base(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return f"https://{host}"


@router.get("/admin/api/stats")
async def stats(request: Request, _=Depends(auth)):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    users, total, rows = await asyncio.gather(
        db.count("users"),
        db.count("downloads", ok="eq.true"),
        db.select("downloads", select="created_at,platform", ok="eq.true", created_at=f"gte.{since}", limit="10000"),
    )
    today = datetime.now(timezone.utc).date()
    per_day = Counter(r["created_at"][:10] for r in rows)
    days = [{"label": (today - timedelta(days=i)).strftime("%d.%m"),
             "count": per_day.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(6, -1, -1)]
    platforms = Counter(r["platform"] for r in rows).most_common(8)
    info, bot = {}, {}
    try:
        info, bot = await asyncio.gather(tg.call("getWebhookInfo"), tg.me())
    except Exception:  # noqa: BLE001
        pass
    return {
        "users": users, "downloads": total, "today": days[-1]["count"], "week": sum(d["count"] for d in days),
        "days": days, "platforms": [{"name": k, "count": v} for k, v in platforms],
        "bot": {"username": bot.get("username"), "name": bot.get("first_name")},
        "webhook": {"url": info.get("url", ""), "pending": info.get("pending_update_count", 0),
                    "error": info.get("last_error_message", "")},
        "expected": f"{_base(request)}/api/webhook",
    }


@router.post("/admin/api/webhook/set")
async def set_webhook(request: Request, _=Depends(auth)):
    await tg.call("setWebhook", url=f"{_base(request)}/api/webhook", secret_token=config.WEBHOOK_SECRET,
                  allowed_updates=["message", "callback_query"], max_connections=40)
    return {"ok": True}


# ───────── настройки: приветствие и подписки ─────────

@router.get("/admin/api/settings")
async def get_settings(_=Depends(auth)):
    return {"welcome": await handlers.get_welcome(),
            "subs_text": await db.get_setting("subs_text", handlers.DEFAULT_SUBS)}


def _clean_welcome(w: dict) -> dict:
    btns = [{"text": str(b.get("text", ""))[:60], "url": str(b.get("url", "")).strip()}
            for b in (w.get("buttons") or []) if b.get("text") and b.get("url")][:4]
    return {"text": str(w.get("text", ""))[:3900], "photo": str(w.get("photo", "")).strip(),
            "effect": str(w.get("effect", "")).strip(), "sticker": str(w.get("sticker", "")).strip(), "buttons": btns}


@router.post("/admin/api/settings")
async def save_settings(body: dict, _=Depends(auth)):
    if "welcome" in body:
        await db.set_setting("welcome", _clean_welcome(body["welcome"]))
    if "subs_text" in body:
        await db.set_setting("subs_text", str(body["subs_text"])[:3900])
    return {"ok": True}


@router.post("/admin/api/welcome/test")
async def test_welcome(body: dict, _=Depends(auth)):
    if not config.ADMIN_IDS:
        raise HTTPException(400, "Укажи свой Telegram ID в переменной ADMIN_IDS")
    await handlers.send_welcome(config.ADMIN_IDS[0], "Админ", _clean_welcome(body.get("welcome", {})))
    return {"ok": True}


@router.post("/admin/api/photo")
async def upload_photo(body: dict, _=Depends(auth)):
    if not config.ADMIN_IDS:
        raise HTTPException(400, "Укажи свой Telegram ID в переменной ADMIN_IDS — фото загружается через бота")
    raw = base64.b64decode(body["data"])
    res = await tg.upload("sendPhoto", {"chat_id": config.ADMIN_IDS[0], "caption": "🖼 Фото для приветствия загружено"},
                          {"photo": ("welcome.jpg", raw)})
    return {"file_id": res["photo"][-1]["file_id"]}


@router.get("/admin/api/photo")
async def photo(id: str, _=Depends(auth)):
    data, ctype = await tg.file_bytes(id)
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


# ───────── обязательные подписки ─────────

@router.get("/admin/api/channels")
async def channels(_=Depends(auth)):
    rows = await db.select("channels", order="id.asc")
    bot = await tg.me()

    async def ok(c):
        try:
            m = await tg.call("getChatMember", chat_id=c["chat_id"], user_id=bot["id"])
            return m["status"] in ("administrator", "creator")
        except tg.TGError:
            return False

    flags = await asyncio.gather(*(ok(c) for c in rows))
    return [{**c, "bot_ok": f} for c, f in zip(rows, flags)]


@router.post("/admin/api/channels")
async def add_channel(body: dict, _=Depends(auth)):
    ref, link = (body.get("ref") or "").strip(), (body.get("link") or "").strip()
    s = re.sub(r"^(https?://)?(t\.me/|telegram\.me/)", "", ref).lstrip("@").split("?")[0].strip("/")
    if not s:
        raise HTTPException(400, "Укажи @username или ID канала")
    if s.startswith("+") or s.startswith("joinchat"):
        raise HTTPException(400, "Для приватного канала укажи его ID (-100…) в первом поле, а пригласительную ссылку — во втором. "
                                 "ID покажет бот, если переслать ему любой пост канала (с админ-аккаунта).")
    chat = await tg.call("getChat", chat_id=int(s) if re.fullmatch(r"-?\d+", s) else "@" + s)
    bot = await tg.me()
    try:
        cm = await tg.call("getChatMember", chat_id=chat["id"], user_id=bot["id"])
    except tg.TGError:
        cm = {"status": "none"}
    if cm["status"] not in ("administrator", "creator"):
        raise HTTPException(400, "Сделай бота администратором этого канала — иначе подписку не проверить")
    username = chat.get("username")
    if not link:
        if username:
            link = f"https://t.me/{username}"
        else:
            try:
                link = (await tg.call("createChatInviteLink", chat_id=chat["id"]))["invite_link"]
            except tg.TGError:
                raise HTTPException(400, "У канала нет публичного @username — укажи пригласительную ссылку")
    await db.insert("channels", {"chat_id": chat["id"], "title": chat.get("title") or s, "username": username,
                                 "link": link, "enabled": True}, on_conflict="chat_id", merge=True)
    return {"ok": True}


@router.patch("/admin/api/channels/{cid}")
async def toggle_channel(cid: int, body: dict, _=Depends(auth)):
    await db.update("channels", {"id": cid}, {"enabled": bool(body.get("enabled"))})
    return {"ok": True}


@router.delete("/admin/api/channels/{cid}")
async def delete_channel(cid: int, _=Depends(auth)):
    await db.delete("channels", {"id": cid})
    return {"ok": True}
