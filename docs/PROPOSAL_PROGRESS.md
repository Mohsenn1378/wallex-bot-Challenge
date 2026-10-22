# PROPOSAL_PROGRESS — اجرای IMPROVEMENT_PROPOSAL v1.1

محیط اجرا: macOS، `/Users/hojjatrezaei/PycharmProjects/wallex-bot`، Python 3.12.15 در `.venv` (numpy، pandas، pyyaml، pytest). دستور تست‌ها: `.venv/bin/python tests/<file>.py`.

| id | وضعیت | زمان (تهران) | نتیجه تست | مشکل / یادداشت |
|---|---|---|---|---|
| P0-00 | بخش ایجنت انجام شد؛ منتظر محسن | 2026-10-22 | T1: `15/15 passed`؛ `git check-ignore .env .cache runs data` ← هر چهار مسیر | `.env` وجود ندارد ← کلید BoxAPI ثبت نشده (کار محسن). تنظیمات برق/Update/QuickEdit ویندوز روی این Mac کاربرد ندارد. |
| P0-01 | done (بخش آفلاین) | 2026-10-22 | T2: `9/9 passed` (pytest هم سبز)؛ T1 `15/15` | T9 (`llm_check --llm box_sol61`) اجرا نشد: `.env`/کلید BoxAPI نیست. `max_completion_tokens` با کلید پروفایل `completion_headroom` (box=2500، بقیه پیش‌فرض قدیمی 4000). سقف دلاری در هر check از env `LLM_BUDGET_USD` خوانده می‌شود (قابل افزایش). بسته بودن بودجه یک درگاه ← پروفایل بعدی زنجیره (اگر دفتر دیگری دارد). `code_version` در `wallex_agent/version.py`. |
| P0-02 | done | 2026-10-22 | T3: `7/7 passed`؛ کل `pytest tests/` سبز | تصمیم امن: کلید ناشناخته (مثلاً غلط تایپی `pause_entry`) کل فایل را نامعتبر می‌کند؛ فایل نامعتبر در اولین بار ← پیش‌فرض‌ها با `pause_entries: true`. ترتیب زمان‌ها هم چک می‌شود (`no_new_entries_after ≤ flatten_start ≤ hard_deadline`). `set_profile` روی `LLMPolicy`، `Native`، `Reflector` (و `Orchestrator`). اعمال در حلقه = P0-06. |
| P0-03 | done | 2026-10-22 | `COMBOS["C7"]` چاپ شد (۹ ارز، size_tiers) | — |
| P0-04 | done | 2026-10-22 | T4: `19/19 passed`؛ T1 `15/15`؛ رگرسیون C1–C6 بیت‌به‌بیت یکسان (`tools/regress_c1_c6.py`، پنجره ثابت ۱۵ روزه ۳ تا ۱۸ اکتبر، hashها در `tests/golden_c1_c6.json` که از موتور قبل از تغییر ضبط شد) | تصمیم امن: `size_mult: 0` ← رد `size_mult_zero` (به جای کف ۶۵k). `control.max_positions` جایگزین کامل `max_pos`. `close_external()` برای بستن‌های بیرون موتور (استاپ سرور/کلاینت) اضافه شد (قرارداد با جریان B). تست رگرسیون به داده `data/candles` آن بازه وابسته است (gitignore)؛ نبود داده ← skip. |
| P0-05 | done | 2026-10-22 | T5: `6/6 passed`؛ payload تصمیم ≈ ۶۱۵ و native ≈ ۱۳۳۴ توکن تخمینی؛ نمونه در `runs/test_prompt_v2/sample_payload.json`؛ کل `pytest tests/` سبز (۵۶) | پرامپت‌ها عیناً از پیوست B. `size_tier`/`setup_quality` در schema مقدار `null` را قبول می‌کنند (skip معتبر)؛ enter بدون tier ← standard در guard. `coin_view` در v2 با ۸ کندل (برای سقف ۱۲۰۰). `allowed_bands` در C7: `size_tiers_tmn` و `max_time_stop_min` = min(1440، تا flatten − ۵). حافظه بازتابی برای C7 هم روشن (۵ درس آخر). |
| P0-06 | pending | | | |
| P0-07 | done (جریان B، subagent) | 2026-10-22 | T6: `13/13 passed` (pytest هم)؛ T1 `15/15` | `WallexClient(api_key, cfg, log_path)` فقط stdlib؛ کلید فقط در هدر و هرگز در `orders.jsonl`. تصمیم‌های امن: `balance_value_includes_locked: null` ← total=value+locked و free=value−locked؛ هر فروش ≤ min(مقدار رکورد، مازاد بر موجودی اولیه، free)؛ لغو استاپ تأیید نشد ← فروش نه (`exit_failed`)؛ نتیجه نامعلوم ورود ← `unknown` + لغو کاغذی + آشتی (خرید دوباره نه)؛ فقط رد صرافی (نه 429/شبکه) در شمارش «دو رد» حساب می‌شود؛ reconcile هیچ سفارشی لغو نمی‌کند. `reconcile()` هر ۵ دقیقه از حلقه اصلی (shadow) صدا زده می‌شود. فیلدهای سفارش (`BUY`/`LIMIT`/`STOP_LIMIT`، `client_id`، `GET /v1/account/orders/{cid}`، لغو `DELETE`) تا تست دود تأیید نشده‌اند. |
| P0-08 | pending | | | |
| P0-09 | اسکریپت آماده؛ اجرا نشده | 2026-10-22 | فقط `py_compile` | `tools/live_smoke.py` بدون `--yes` اجرا نمی‌شود (exit 2)؛ `--tmn` در (50k، 120k]؛ `config/broker.yaml` پیش‌فرض امن پیوست A4 (`stop_supported: false`). **اجرا فقط با تأیید صریح محسن در همان لحظه و با `WALLEX_API_KEY` در `.env`.** پیشنهاد subagent: اگر استاپ با پیکربندی واقعی رد شد، فعلاً `limit_rel_ok: false` و `stop_supported` می‌ماند true (یعنی همیشه fallback به استاپ کلاینت). |
| P0-10 | pending | | | |
| P0-11 | pending | | | |

## یادداشت‌های P0-00

- کپی مخزن روی این Mac هیچ dotfileی نداشت (`.git`، `.gitignore`، `.env`، `.cache/`). نتیجه:
  - `git init` تازه + commit پایه `6951a3d`؛ branch `live-c7` از روی آن. **تاریخچه با GitHub (`Mohsenn1378/wallex-bot`، commit `5897f87`) یکی نیست**؛ قبل از push باید تصمیم گرفته شود (rebase روی مخزن اصلی یا جایگزینی `.git`).
  - `.gitignore` از نو نوشته شد (الگوهای ریشه: `/.cache`، `/runs`، `/data`؛ با اسلش اول تا پکیج `wallex_agent/data` ignore نشود).
  - دفتر توکن قدیمی (`.cache/token_ledger.txt`، ~۶۴۷ هزار توکن مصرف‌شده) اینجا نیست؛ دفتر CodeCraft روی این ماشین از صفر شروع می‌شود.
- Python سیستم 3.9.6 بود (کمتر از 3.10 لازم)؛ `.venv` با Python 3.12 ساخته شد.
- `.env.example` ← `WALLEX_API_KEY=`، `BOXAPI_API_KEY=`، `CODECRAFT_API_KEY=`.
