import json

import httpx

from . import config


class TGError(Exception):
    def __init__(self, desc: str, code: int = 0):
        super().__init__(desc)
        self.desc = desc
        self.code = code


def _url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"


def _result(r: httpx.Response):
    d = r.json()
    if not d.get("ok"):
        raise TGError(d.get("description", "Telegram error"), d.get("error_code", 0))
    return d["result"]


async def call(method: str, **params):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_url(method), json={k: v for k, v in params.items() if v is not None})
    return _result(r)


async def send(method: str, **params):
    """Как call, но при ошибке HTML-разметки повторяет без parse_mode."""
    try:
        return await call(method, **params)
    except TGError as e:
        if "parse entities" in e.desc.lower() and params.get("parse_mode"):
            params.pop("parse_mode")
            return await call(method, **params)
        raise


async def upload(method: str, data: dict, files: dict):
    form = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in data.items() if v is not None}
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as c:
        r = await c.post(_url(method), data=form, files=files)
    return _result(r)


async def file_bytes(file_id: str) -> tuple[bytes, str]:
    info = await call("getFile", file_id=file_id)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{info['file_path']}")
    return r.content, r.headers.get("content-type", "image/jpeg")


_me: dict | None = None


async def me() -> dict:
    global _me
    if _me is None:
        _me = await call("getMe")
    return _me
