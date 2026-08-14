# Instagram → Telegram Bot (v1)

نسخه اول برای استفاده عمومی با یک Instagram admin session و ارسال خروجی فقط به همان کاربری است که لینک را فرستاده است.

## امکانات

- Post / Reel / TV post
- Carousel
- Story با لینک مستقیم
- Highlight با لینک مستقیم
- سقف 5 درخواست در هر ساعت برای هر کاربر
- حداقل 10 ثانیه فاصله بین درخواست‌ها
- جلوگیری از لینک تکراری
- صف دانلود در Redis/Valkey
- بازیابی jobهای نیمه‌تمام بعد از restart
- Retry محدود برای خطاهای موقت
- دانلود فایل به‌صورت stream به‌جای نگه‌داشتن کل فایل در RAM
- timeout برای درخواست‌های شبکه
- session اینستاگرام خارج از کد و به‌صورت base64 در secret
- پاک کردن فایل‌های موقت بعد از ارسال به تلگرام
- health endpoint در `/health`

## نکته مهم درباره Session

`INSTAGRAM_SESSION_B64` باید base64 محتوای فایل session خود Instaloader باشد، نه رمز عبور Instagram.

ساخت session روی سیستم شخصی:

```bash
pip install instaloader==4.15.3
instaloader --login YOUR_INSTAGRAM_USERNAME
```

بعد فایل session ساخته‌شده توسط Instaloader را base64 کنید:

Linux/macOS:

```bash
base64 -w 0 ~/.config/instaloader/session-YOUR_INSTAGRAM_USERNAME
```

macOS در بعضی محیط‌ها:

```bash
base64 ~/.config/instaloader/session-YOUR_INSTAGRAM_USERNAME | tr -d '\n'
```

مقدار خروجی را فقط در secret/environment provider قرار دهید. آن را commit نکنید.

## اجرای محلی

```bash
cp .env.example .env
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

برای Redis محلی می‌توانید از Docker استفاده کنید:

```bash
docker run --rm -p 6379:6379 redis:7-alpine
```

و در `.env`:

```env
REDIS_URL=redis://localhost:6379/0
```

## Koyeb + GitHub

پیشنهاد فعلی برای نسخه رایگان: Koyeb برای سرویس وب + Upstash Redis برای queue/rate limiting.

در Koyeb یک Web Service از GitHub بسازید، Dockerfile را انتخاب کنید و environment variables فایل `.env.example` را در Dashboard وارد کنید.

`WEBHOOK_URL` باید URL عمومی HTTPS سرویس باشد، مثلاً:

```env
WEBHOOK_URL=https://your-service.koyeb.app
```

وبهوک واقعی توسط برنامه روی این مسیر ثبت می‌شود:

```text
https://your-service.koyeb.app/telegram
```

## Render

فایل `render.yaml` برای یک Web Service رایگان و یک Key Value رایگان آماده شده است. مقادیر secret را در Dashboard وارد کنید.

`WEBHOOK_URL` باید آدرس عمومی سرویس Render باشد، مثلاً:

```env
WEBHOOK_URL=https://your-service.onrender.com
```

## محدودیت‌های نسخه رایگان

این نسخه عمداً یک process دارد: HTTP webhook + download worker. این کار برای محدودیت سرویس‌های رایگان مناسب است، ولی برای production واقعی باید worker را جدا کنید و datastore پایدارتر داشته باشید.

## نکته Highlight

Story و Highlight به session معتبر نیاز دارند. برای Highlight مستقیم، Instagram endpoint داخلی استفاده می‌شود؛ این قسمت API رسمی عمومی Instagram نیست و ممکن است با تغییرات Instagram نیاز به به‌روزرسانی داشته باشد.
