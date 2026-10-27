"""Минимальный клиент Supabase (PostgREST) на httpx."""
import httpx

from . import config


def _headers(prefer: str | None = None) -> dict:
    h = {
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


async def _req(method: str, table: str, params=None, body=None, prefer=None) -> httpx.Response:
    async with httpx.AsyncClient(base_url=f"{config.SUPABASE_URL}/rest/v1", timeout=20) as c:
        r = await c.request(method, f"/{table}", params=params, json=body, headers=_headers(prefer))
    if r.status_code >= 300:
        raise RuntimeError(f"База данных: {method} {table} → {r.status_code} {r.text[:200]}")
    return r


async def select(table: str, **params) -> list[dict]:
    return (await _req("GET", table, params=params)).json()


async def insert(table: str, rows, on_conflict: str | None = None, ignore=False, merge=False) -> list[dict]:
    pref = "return=representation"
    if ignore:
        pref += ",resolution=ignore-duplicates"
    if merge:
        pref += ",resolution=merge-duplicates"
    params = {"on_conflict": on_conflict} if on_conflict else None
    return (await _req("POST", table, params, rows, pref)).json()


async def update(table: str, match: dict, values: dict) -> None:
    await _req("PATCH", table, {k: f"eq.{v}" for k, v in match.items()}, values)


async def delete(table: str, match: dict) -> None:
    await _req("DELETE", table, {k: f"eq.{v}" for k, v in match.items()})


async def count(table: str, **params) -> int:
    r = await _req("GET", table, {**params, "select": "*", "limit": 1}, prefer="count=exact")
    return int(r.headers.get("content-range", "*/0").split("/")[-1] or 0)


async def get_setting(key: str, default):
    rows = await select("settings", key=f"eq.{key}")
    return rows[0]["value"] if rows else default


async def set_setting(key: str, value) -> None:
    await insert("settings", {"key": key, "value": value}, on_conflict="key", merge=True)
