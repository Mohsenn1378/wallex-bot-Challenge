# PROPOSAL_PROGRESS — اجرای IMPROVEMENT_PROPOSAL v1.1

محیط اجرا: macOS، `/Users/hojjatrezaei/PycharmProjects/wallex-bot`، Python 3.12.15 در `.venv` (numpy، pandas، pyyaml، pytest). دستور تست‌ها: `.venv/bin/python tests/<file>.py`.

| id | وضعیت | زمان (تهران) | نتیجه تست | مشکل / یادداشت |
|---|---|---|---|---|
| P0-00 | بخش ایجنت انجام شد؛ منتظر محسن | 2026-10-22 | T1: `15/15 passed`؛ `git check-ignore .env .cache runs data` ← هر چهار مسیر | `.env` وجود ندارد ← کلید BoxAPI ثبت نشده (کار محسن). تنظیمات برق/Update/QuickEdit ویندوز روی این Mac کاربرد ندارد. |
| P0-01 | done (بخش آفلاین) | 2026-10-22 | T2: `9/9 passed` (pytest هم سبز)؛ T1 `15/15` | T9 (`llm_check --llm box_sol61`) اجرا نشد: `.env`/کلید BoxAPI نیست. `max_completion_tokens` با کلید پروفایل `completion_headroom` (box=2500، بقیه پیش‌فرض قدیمی 4000). سقف دلاری در هر check از env `LLM_BUDGET_USD` خوانده می‌شود (قابل افزایش). بسته بودن بودجه یک درگاه ← پروفایل بعدی زنجیره (اگر دفتر دیگری دارد). `code_version` در `wallex_agent/version.py`. |
| P0-02 | pending | | | |
| P0-03 | pending | | | |
| P0-04 | pending | | | |
| P0-05 | pending | | | |
| P0-06 | pending | | | |
| P0-07 | pending | | | |
| P0-08 | pending | | | |
| P0-09 | pending | | | فقط با تأیید صریح محسن در همان لحظه |
| P0-10 | pending | | | |
| P0-11 | pending | | | |

## یادداشت‌های P0-00

- کپی مخزن روی این Mac هیچ dotfileی نداشت (`.git`، `.gitignore`، `.env`، `.cache/`). نتیجه:
  - `git init` تازه + commit پایه `6951a3d`؛ branch `live-c7` از روی آن. **تاریخچه با GitHub (`Mohsenn1378/wallex-bot`، commit `5897f87`) یکی نیست**؛ قبل از push باید تصمیم گرفته شود (rebase روی مخزن اصلی یا جایگزینی `.git`).
  - `.gitignore` از نو نوشته شد (الگوهای ریشه: `/.cache`، `/runs`، `/data`؛ با اسلش اول تا پکیج `wallex_agent/data` ignore نشود).
  - دفتر توکن قدیمی (`.cache/token_ledger.txt`، ~۶۴۷ هزار توکن مصرف‌شده) اینجا نیست؛ دفتر CodeCraft روی این ماشین از صفر شروع می‌شود.
- Python سیستم 3.9.6 بود (کمتر از 3.10 لازم)؛ `.venv` با Python 3.12 ساخته شد.
- `.env.example` ← `WALLEX_API_KEY=`، `BOXAPI_API_KEY=`، `CODECRAFT_API_KEY=`.
