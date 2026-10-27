import hashlib
import os


def _ids(v: str) -> list[int]:
    return [int(x) for x in v.replace(" ", "").split(",") if x.lstrip("-").isdigit()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
YANDEX_TOKEN = os.getenv("YANDEX_TOKEN", "")
COOKIES_B64 = os.getenv("COOKIES_B64", "")  # cookies.txt в base64 (Instagram и др.)
MAX_MB = 50

_seed = hashlib.sha256(f"{BOT_TOKEN}:{ADMIN_PASSWORD}".encode()).hexdigest()
SESSION_SECRET = os.getenv("SESSION_SECRET") or _seed
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or _seed[:40]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def public_url() -> str:
    u = os.getenv("PUBLIC_URL") or os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL") or ""
    if u and not u.startswith("http"):
        u = "https://" + u
    return u.rstrip("/")
