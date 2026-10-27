# 📥 Бот-загрузчик (Vercel + Supabase)

Telegram-бот на вебхуках + красивая админ-панель (`/admin`): приветствие, обязательные подписки, статистика.

## Деплой за 6 шагов
1. **Supabase**: создай проект → SQL Editor → выполни `schema.sql`. Скопируй `Project URL` и `service_role` ключ.
2. **Бот**: @BotFather → `/newbot` → токен. Свой Telegram ID узнай у @userinfobot.
3. **GitHub**: залей папку проекта в репозиторий.
4. **Vercel**: Add New → Project → выбери репозиторий. В Environment Variables добавь переменные из `.env.example`:
   `BOT_TOKEN`, `ADMIN_PASSWORD`, `ADMIN_IDS`, `WEBHOOK_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (`YANDEX_TOKEN` и `COOKIES_B64` — по желанию, можно пропустить). Deploy.
5. **Вебхук**: открой `https://ТВОЙ-ПРОЕКТ.vercel.app/admin` → войди → «Обзор» → **«Установить вебхук»**.
   Ссылка вебхука: `https://ТВОЙ-ПРОЕКТ.vercel.app/api/webhook`
   Вручную: `https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://ТВОЙ-ПРОЕКТ.vercel.app/api/webhook&secret_token=<WEBHOOK_SECRET>`
6. Напиши боту `/start` — готово. Админам доступна команда `/admin` со ссылкой на панель.

## Админ-панель
- **Приветствие**: текст (HTML, `{name}`), фото, анимация сообщения (🔥🎉❤️…), анимированный стикер, анимированные эмодзи, кнопки, живой предпросмотр и тест в личку.
- **Подписки**: добавь канал (@username / ID), бот должен быть там админом. Перед скачиванием бот проверяет подписку и показывает кнопки каналов + «Я подписался»; после проверки сам скачивает ссылку.
- **Подсказки для админов**: перешли боту стикер или сообщение с анимированным эмодзи → он пришлёт ID; перешли пост канала → пришлёт ID канала.

## Ограничения Vercel
- Нет **ffmpeg**: SoundCloud отдаётся в исходном mp3/m4a без перекодирования, видео — готовые mp4 без склейки.
- Функция живёт до 60 с (`vercel.json`, можно поднять до 300 на Fluid compute); файл ≤ 50 МБ.
- Instagram/YouTube часто блокируют IP дата-центров — для Instagram положи cookies.txt в `COOKIES_B64` (`base64 -w0 cookies.txt`).
- Для тяжёлого трафика лучше VPS/Docker. Обновляй `yt-dlp` (передеплой подтянет свежий).
