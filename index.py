import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from bot import config, handlers
from bot.admin import router as admin_router

logging.basicConfig(level=logging.INFO)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(admin_router)


@app.exception_handler(Exception)
async def on_error(_: Request, exc: Exception):
    return JSONResponse({"detail": str(exc) or exc.__class__.__name__}, status_code=500)


@app.post("/api/webhook")
async def webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != config.WEBHOOK_SECRET:
        return Response(status_code=403)
    update = await request.json()
    try:
        await handlers.handle_update(update)
    except Exception:  # noqa: BLE001
        logging.exception("update failed")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def root():
    return "<body style='font:16px system-ui;display:grid;place-items:center;height:100vh'>📥 Bot is running · <a href='/admin'>admin</a></body>"
