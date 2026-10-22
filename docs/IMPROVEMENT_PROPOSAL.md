---
doc: docs/IMPROVEMENT_PROPOSAL.md
version: "1.1"
created: "2026-10-22 20:30 Tehran"
revised: "2026-10-22 20:50 Tehran — v1.1 after independent verification review (25 findings applied)"
author: "Claude (Cowork) — panel review: 3 AI + 3 markets + 3 software experts"
implementer: "Claude Code on Windows PC 'rajman' (has network access to Wallex + BoxAPI)"
repo: "C:\\Users\\rajman\\Documents\\wallex-bot  (GitHub: Mohsenn1378/wallex-bot)"
go_live_target: "2026-10-22 23:00 Tehran (±10 min)" # cut-line: 23:15 (MVL, see §8)
work_streams: {A: [P0-01, P0-02, P0-03, P0-04, P0-05, P0-06], B: [P0-07, P0-09, P0-08, P0-10], then: [P0-11]}  # A and B in parallel
window_end: "2026-10-23 23:00 Tehran"              # contest end — bot must be flat in TMN before this
flatten: {start: "2026-10-23 22:00", hard_deadline: "2026-10-23 22:45"}
capital_tmn: 5000000
objective: "max net TMN profit with low drawdown in ~24h; 10-15 sensible trades / 24h"
llm:
  provider: BoxAPI
  base_url: "https://ai.boxapi.ir/ai/v1"           # OpenAI-compatible: /chat/completions
  key_env: BOXAPI_API_KEY                          # .env only — never in chat/git/logs
  primary: gpt-6.1-sol                             # reasoning_effort: low
  fallbacks: [gpt-6-sol, gpt-5.6-sol]              # strong models only (user: no weak model)
  budget_usd: 3.0                                  # ledger cap 2.7; hot-swappable at runtime
user_decisions:                                    # confirmed in Q&A, 2026-10-22 19:30-20:00
  - "one unified agent (C7) on the full 5,000,000 TMN; keep >=50% cash"
  - "live LLM = BoxAPI gpt-6.1-sol, CodeCraft NOT used live; model must be hot-swappable during the run"
  - "SL 4-5% per position + per-trade loss cap via size (~0.3% of equity ≈ 15k TMN)"
  - "soft drawdown governor (-2% half size, -4% manage-only), no forced liquidation"
  - "activity 10-15 trades/24h: medium setups enter with PROBE size; LLM free scan every 30 min on 9 liquid coins"
  - "start on time with a safe core; further improvements via controlled restarts"
  - "real-order smoke test (~60k TMN) before go-live: YES"
  - "TMN markets only (no USDT pairs this run)"
tasks_index:
  P0: [P0-00, P0-01, P0-02, P0-03, P0-04, P0-05, P0-06, P0-07, P0-08, P0-09, P0-10, P0-11]
  P1: [P1-01, P1-02, P1-03, P1-04, P1-05, P1-06]
  P2: [P2-01, P2-02, P2-03, P2-04, P2-05]
progress_file: docs/PROPOSAL_PROGRESS.md
---

# پروپوزال نهایی‌سازی ایجنت معاملاتی والکس — اجرای واقعی ۲۴ ساعته

> **برای انسان:** بخش‌های ۱ تا ۵ را بخوانید (۱۰ دقیقه). بخش ۸ برنامه زمانی و نقطه‌های تصمیم شماست.
> **برای ایجنت (Claude Code):** اول «دستور کار ایجنت» (همین پایین)، بعد بخش ۶ (تسک‌ها) را به ترتیب اجرا کن؛ بخش ۷ تست‌ها و بخش ۱۲ پیوست‌ها (پرامپت‌ها و کانفیگ‌ها) مرجع پیاده‌سازی‌اند.

## دستور کار ایجنت (Agent Operating Rules)

1. این فایل را کامل بخوان. ترتیب اجرا: `P0-00`، سپس **دو جریان موازی** — جریان A (`P0-01 → P0-06`: لایه LLM، کنترل، C7، گاردریل، پرامپت، حلقه) و جریان B (`P0-07 → P0-09 → P0-08 → P0-10`: کلاینت والکس، اجراکننده، تست دود، ادامه بعد از ری‌استارت) با یک subagent یا worktree جدا؛ قرارداد بین دو جریان همان رابط hookها در P0-04 بند ۷ و ۸ است — بعد `P0-11`، بعد `P1`، بعد `P2`. هر تسک یک بلوک YAML دارد (`id`, `files`, `depends_on`, `est_min`) و یک «معیار پذیرش».
2. بعد از هر تسک: تست‌های همان تسک را اجرا کن، نتیجه را در `docs/PROPOSAL_PROGRESS.md` بنویس (`id`, وضعیت، زمان، خروجی تست، مشکل)، و commit کن (`git commit -m "P0-xx: ..."`). اگر کار قطع شد، از روی همین فایل ادامه بده.
3. **کلیدها:** `WALLEX_API_KEY` و `BOXAPI_API_KEY` فقط در `.env`. هرگز چاپ، لاگ یا commit نشوند. در لاگ سفارش‌ها هدر `x-api-key` حذف شود.
4. **سفارش واقعی:** فقط در `P0-09` (تست دود) و بعد از تأیید صریح محسن در همان لحظه، و در اجرای واقعی توسط خود ربات. هیچ سفارش دستی دیگری ثبت نکن.
5. **رفتار کمبوهای قدیمی (C1–C6) نباید عوض شود.** همه تغییرات موتور پشت کلیدهای کانفیگ C7 یا hookهای اختیاری باشند (پیش‌فرض خاموش).
6. خط برش (بخش ۸.۲) را رعایت کن: اگر در ساعت‌های مشخص‌شده چیزی آماده نشد، با نسخه حداقلی (MVL) شروع کن و بقیه را بعداً با ری‌استارت کنترل‌شده اضافه کن. MVL هیچ‌وقت بدون آشتی/adopt هنگام شروع، استاپ کلاینت و بستن بر اساس موجودی شروع نمی‌شود.
8. **ایمنی گیت:** فقط با مسیر صریح stage کن؛ قبل از هر push، `git check-ignore .env` باید `.env` را چاپ کند.
7. هر ابهام فنی که تصمیمش در این سند نیست: امن‌ترین گزینه (ورود نکردن / حفظ استاپ) را انتخاب کن و در `PROPOSAL_PROGRESS.md` ثبت کن.

---

## ۱. خلاصه اجرایی

**تصمیم پنل:** به جای ۶ کمبو، **یک ایجنت واحد (C7)** روی کل ۵ میلیون اجرا شود که سه چیز را از شواهد سه تست برداشته است: خرید افت عمیق (M1) فقط روی ۹ ارز نقد تومانی، LLM به‌عنوان تصمیم‌گیرنده واقعی با **پرامپت کالیبره‌شده با شواهد**، و اسکن آزاد LLM هر ۳۰ دقیقه (مثل C5 که تنها برد زنده را داشت).

**چهار مشکل اصلی که این پروپوزال حل می‌کند:**

| # | مشکل | شاهد | راه‌حل |
|---|---|---|---|
| 1 | **بروکر واقعی وجود ندارد** و بیشتر ارزها اصلاً سفارش Market ندارند | `isMarketTypeEnable=false` برای PEPE، PUMP، SOL، DOGE، BNB، ARB، S | اجراکننده آینه‌ای (mirror) با Limit تهاجمی روی دفتر سفارش + استاپ سمت سرور + آشتی |
| 2 | **LLM تقریباً همه کارت‌ها را رد می‌کند** و دلیل‌هایش با داده نمی‌خواند | «thin/illiquid»، «downtrend»، «target < costs» در اکثر ردها؛ ولی داخل همین ۹ ارز، نقدشوندگی پایین‌تر و رژیم نزولی بدتر نبودند | پرامپت v2 با جدول شواهد، ویژگی‌های نسبی، حذف فیلد گمراه‌کننده، سطح‌بندی سایز به جای رد |
| 3 | **منطق ورود روی «اولین» سیگنال است** که بدترین سیگنال است | اولین کارت M1 در ۶ ساعت: ‎−۰٫۳۲٪‎ هر معامله؛ کارت تکراری: ‎+۰٫۷۴٪‎؛ افت ≥۸٪ از سقف ۶ ساعته: ‎+۳٫۵٪‎ (در هر دو نیمه داده پایدار) | «مسلح کن، بعد ماشه بکش»: اولین سیگنال فقط ارز را مسلح می‌کند؛ ورود روی سیگنال تکراری یا افت عمیق |
| 4 | **ریسک و زمان پایان مدیریت نمی‌شوند** | `dd_cap` در کد اعمال نمی‌شود؛ توقف روزانه نیمه‌شب صفر می‌شود؛ وضعیت بعد از ری‌استارت از دست می‌رود؛ سقف توکن مشترک CodeCraft فراخوانی‌های BoxAPI را هم می‌بندد | تنظیم‌کننده افت نرم، سقف ریسک هر معامله، سقف گرمای پرتفوی، آگاهی از پایان پنجره، ذخیره وضعیت، دفتر هزینه دلاری جدا |

**انتظار واقع‌بینانه (صادقانه):** نسخه قانون‌محور همین منطق روی ۲۵ روز داده: ‎−۰٫۲۳٪‎ (در برابر ‎−۳٫۱۱٪‎ منطق فعلی)، بیشترین افت ۳٫۰٪ (در برابر ۴٫۸٪)، و روی پنجره‌های ۲۴٫۵ ساعته شبیه همین مسابقه: میانه ۰٫۰٪، بدترین ‎−۱٫۱٪‎، بدترین افت ۱٫۳٪. یعنی **قواعد ثابت لبه پایدار ندارند**؛ لبه باید از انتخاب‌گری LLM کالیبره‌شده و سایزدهی بر اساس کیفیت بیاید. هدف عملی ۲۴ ساعت: **بازده مثبت با افت زیر ۲٪ و ۱۰ تا ۱۵ معامله.** پیش‌بینی سود قطعی ممکن نیست؛ کنترل ضرر قطعی است.

**هزینه LLM:** حدود ۰٫۸ تا ۱٫۵ دلار برای ۲۴ ساعت (حدود ۵۰ تا ۷۰ فراخوانی با gpt-6.1-sol، تلاش استدلال low). ۳ دلار کافی است؛ شارژ ۲ دلار اضافه اختیاری است (بیمه در برابر قیمت‌گذاری ناشناخته BoxAPI).

---

## ۲. هدف‌ها، محدودیت‌ها و «تعریف انجام‌شده»

**هدف (از فایل‌های چالش + پاسخ‌های شما):** بیشترین سود خالص تومانی با کمترین افت، در حدود ۲۴ ساعت باقی‌مانده؛ با حفظ امتیازهای فنی (۱۵۰ امتیاز: منطق، AI واقعی، مدیریت ریسک، تکرارپذیری، بک‌تست/سایه).

| محدودیت | مقدار |
|---|---|
| پایان پنجره | جمعه ۱ آبان ۱۴۰۵ ساعت ۲۳:۰۰ تهران — باید همه‌چیز تومان باشد |
| سرمایه | ۵,۰۰۰,۰۰۰ تومان، فقط اسپات تومانی، حداقل ۵۰٪ نقد |
| حداقل سفارش | ۵۰,۰۰۰ تومان روی خرید و فروش؛ پله مقدار هر بازار |
| سفارش Market | از ۱۸۷ بازار اسپات تومانی فقط BTC، ETH، USDT و XAUT؛ بقیه فقط Limit. سفارش دورتر از ۴٪ (حداکثر ۸٪) از «قیمت منصفانه» ممکن است رد شود؛ PUMP برچسب `isRisky` دارد |
| کارمزد | Maker ۰٫۲۵٪، Taker ۰٫۳۰٪؛ رفت‌وبرگشت واقعی ~۰٫۸–۱٪ |
| مدل | BoxAPI، gpt-6.1-sol؛ قابل تعویض در حین اجرا؛ فقط مدل‌های قوی |
| بودجه LLM | ۳ دلار |
| قوانین | همه معاملات الگوریتمی؛ لاگ دلیل هر معامله؛ کد روی GitHub همان نسخه‌ای که معامله کرده |

**تعریف انجام‌شده (Go-Live):** همه تست‌های آفلاین سبز · `llm_check` روی `box_sol61` آماده · تست دود واقعی موفق و `config/broker.yaml` ساخته شده · اجرای خشک ۱۰ دقیقه‌ای بدون خطا · commit و tag `live-v1` · تنظیمات برق ویندوز (بدون Sleep).

---

## ۳. یافته‌های کلیدی (شواهد)

### ۳.۱ بازبینی کد

| یافته | محل | اثر | تسک |
|---|---|---|---|
| Market روی ۷ ارز از ۹ ارز نقد بسته است؛ گزارش فرض کرده همه Market دارند | خروجی `/v1/markets` و `wallex_check_result.json` | بروکری که Market بفرستد روی PEPE/PUMP/... رد می‌شود | P0-07 |
| `dd_cap` («سقف افت هدف») فقط در حکم گزارش (`report.py`) استفاده می‌شود و در معامله اعمال نمی‌شود | `engine.py` (`cid_dd_pause: pass`) | کنترل افت سطح حساب وجود ندارد | P0-04 |
| توقف روزانه در نیمه‌شب تهران صفر می‌شود | `Account.on_bar` | در پنجره ۲۴ ساعته (پنجشنبه→جمعه) عملاً دو بار فرصت ضرر | P0-04 |
| `TokenLedger` برای همه پروفایل‌ها مشترک است و سقف `token_budget_total: 900000` دارد؛ ۶۴۷ هزار مصرف شده | `llm/adapter.py` | بعد از ~۲۵۲ هزار توکن، **فراخوانی‌های BoxAPI هم بسته می‌شوند** | P0-01 |
| `--llm sol56` زنجیره نقش‌ها را غیرفعال می‌کند (reflect هم با Sol اجرا می‌شود؛ fallback نیست) | `LLM.from_config` | timeout مستقیم به «رد کارت» | P0-01 |
| وضعیت سایه ذخیره نمی‌شود؛ و `LiveFeed.lo` در هر ری‌استارت عوض می‌شود ← همه اندیس‌های ذخیره‌شده (entry_idx، cooldown، pending) جابه‌جا می‌شوند | `shadow.py` | ری‌استارت = گم شدن پوزیشن‌ها | P0-08 |
| C5 native و ارکستراتور حتی وقتی ظرفیت ورود نیست فراخوانی می‌شوند | `Shadow.step_account` | هدر رفت بودجه | P0-06 |
| `_direct` پروفایل را روی "C5" هاردکد کرده | `shadow.py` | برای C7 قابل استفاده نیست | P0-06 |
| فیلد `natural_target_pct_above` (BB-mid/VWAP) به‌عنوان «هدف» خوانده می‌شود | `ContextBuilder.card` | دلیل «target below costs» در ده‌ها رد | P0-05 |
| `share_of_minutes_traded` مقیاس مرجع ندارد (BTC با ۰٫۶۸ نقدترین بازار والکس است) | `ContextBuilder.coin_view` | دلیل «thin/illiquid» حتی برای BTC و ETH | P0-05 |

### ۳.۲ شواهد کمّی (۳۵ روز داده ۱ دقیقه، ۹ ارز نقد، شبیه‌ساز بدبینانه پروژه)

اسکریپت‌ها: `tools/evidence/a3..a9.py` (پیوست G). همه اعداد «خالص هر معامله» با پلن SL ۵٪ / TP ۶٪ / نگهداری حداکثر ۲۴ ساعت و کارمزد، اسپرد ×۱٫۵ و لغزش.

| شرط کارت M1 (BTC در حال سقوط نیست) | تعداد | خالص هر معامله | برد | نیمه اول / دوم داده |
|---|---|---|---|---|
| همه | ۲۳۳ | ‎+۰٫۲۲٪‎ | ۵۴٪ | +۰٫۶۴ / −۰٫۴۱ |
| **اولین سیگنال روی ارز در ۶ ساعت** | ۱۱۴ | **‎−۰٫۳۲٪‎** | ۴۶٪ | −۰٫۳۵ / −۰٫۲۸ |
| **سیگنال تکراری (≥۱ سیگنال قبلی در ۶ ساعت)** | ۱۱۹ | **‎+۰٫۷۴٪‎** | ۶۱٪ | +۰٫۹۹ / +۰٫۴۵ |
| ۲ تا ۳ سیگنال قبلی | ۵۱ | ‎+۱٫۵۴٪‎ | ۶۹٪ | +۲٫۱۱ / +۰٫۹۴ |
| **قیمت ≥۸٪ زیر سقف ۶ ساعته** | ۳۰ | **‎+۳٫۵۲٪‎** | ۸۷٪ | +۳٫۳۰ / +۳٫۶۵ |
| BTC بالای EMA50 یک‌ساعته | ۴۷ | ‎+۱٫۲۹٪‎ | ۶۶٪ | +۰٫۷۲ / +۱٫۶۱ |
| ساعت ۱۷ تا ۲۳ تهران | ۷۲ | ‎+۱٫۰۵٪‎ | ۶۱٪ | +۰٫۴۳ / +۱٫۶۱ |
| ساعت ۰ تا ۵ تهران | ۶۵ | ‎−۰٫۴۹٪‎ | ۴۳٪ | +۰٫۱۸ / −۰٫۷۸ |
| (حذف‌شده با کد) BTCUSDT در ۱۵ دقیقه ≤ ‎−۰٫۵٪‎ | ۴۴ | ‎−۱٫۴۴٪‎ | — | — |

| پلن خروج (همه کارت‌های بالا) | خالص هر معامله |
|---|---|
| SL ۵٪، TP ۶٪، ۲۴ ساعت | **‎+۰٫۲۲٪‎** |
| SL ۳٪ (یا ATR ≈ ۳٪) | ‎−۰٫۱۴ تا −۰٫۲۹٪‎ |
| SL ۲٫۵٪ | ‎−۰٫۳۸٪‎ |
| نگهداری ۱۲ / ۸ / ۴ ساعت | +۰٫۰۶ / +۰٫۰۲ / **−۰٫۴۰٪** |
| BE یا trailing | بدتر از بدون آن‌ها |

| پرتفوی (موتور واقعی، ۵M، حداکثر ۶ پوزیشن) | ۲۵ روز | افت | PF | معامله/روز | پنجره‌های ۲۴٫۵ ساعته (از ۲۲:۳۰) |
|---|---|---|---|---|---|
| منطق فعلی (ورود روی اولین سیگنال) | ‎−۳٫۱۱٪‎ | ۴٫۷۷٪ | ۰٫۷۱ | ۳٫۴ | میانه −۰٫۰۱، بدترین −۱٫۵۳ |
| **مسلح کن، بعد ماشه (پیشنهادی)** | **‎−۰٫۲۳٪‎** | **۳٫۰۱٪** | **۰٫۹۵** | ۲٫۰ | میانه ۰٫۰۰، بدترین −۱٫۰۹، بدترین افت ۱٫۳۱ |

نکته‌های تکمیلی: USDT/TMN در ۳۵ روز ‎+۱۸٫۹٪‎ (میانگین ‎+۰٫۵٪‎ در روز، انحراف معیار ۱٫۵۵٪)؛ رانش تصادفی ارزهای نقد ‎+۰٫۵۶٪‎ ناخالص در ۲۴ ساعت (بعد از هزینه ≈ صفر). M4، M2 و M7 با هیچ پلن خروجی خالص مثبت نشدند.

**هشدار روش‌شناختی:** این اعداد درون‌نمونه‌اند و پلن SL۵/TP۶ از بین ~۱۱ گزینه انتخاب شده. فقط ردیف‌هایی که در **هر دو نیمه** داده هم‌جهت بودند به‌عنوان قاعده کد استفاده می‌شوند (سیگنال تکراری، افت عمیق، فیلتر سقوط BTC). بقیه فقط به‌عنوان «شواهد» به LLM داده می‌شوند، نه قاعده.

---

## ۴. اتاق متخصصان

### ۴.۱ متخصصان هوش مصنوعی

**AI-1 — کالیبراسیون تصمیم (Decision Science)**
- ۸۰٪ ردهای تست B سه دلیل دارند که همه «مصنوعی» یا غلط‌کالیبره‌اند: «thin trading» (۰٫۲۵ تا ۰٫۷ برای این ۹ ارز عادی است)، «natural target < costs» (هدف واقعی ۵–۷٪ است، نه BB-mid)، و «downtrend» (ستاپ افت ذاتاً زیر EMA50 و با ADX بالاست).
- مدل در ردهایش تقریباً تصادفی بود (دقت رد ۲۹–۵۷٪ ≈ نرخ پایه) ولی در پذیرش‌های کمش خوب بود (p=۰٫۰۳۵ و ۰٫۰۰۷ در برابر shuffle). یعنی **توان قضاوت دارد، کالیبراسیون ندارد.**
- پیشنهاد: پرامپت v2 با جدول شواهد کوتاه، ویژگی‌های نسبی (رتبه نقدشوندگی در کل بازار، افت از سقف ۶ ساعته، تعداد سیگنال‌های ۶ ساعت اخیر، ساعت تهران، ساعت‌های باقی‌مانده)، و یک روبریک امتیازدهی `setup_quality` ۰–۱۰۰ که مستقیم به سطح سایز نگاشت می‌شود. فیلد هدف گمراه‌کننده به `nearby_resistance_pct` با توضیح «هدف معامله نیست» تغییر نام کند.

**AI-2 — معماری ایجنت و اقتصاد فراخوانی**
- با قیمت عمومی gpt-6.1-sol (ورودی ۲، خروجی ۱۰ دلار در میلیون) هر تصمیم ≈ ۰٫۰۱–۰٫۰۲۵ دلار. ۳ دلار ≈ ۱۲۰ تا ۳۰۰ فراخوانی؛ کافی است به شرط اینکه کد قبل از LLM فیلتر کند (BTC، مسلح‌سازی، ظرفیت) و اسکن آزاد وقتی ظرفیت نیست اجرا نشود.
- دفتر توکن فعلی مشترک است ← BoxAPI با سقف CodeCraft بسته می‌شود. باید **دفتر جدا برای هر درگاه + دفتر هزینه دلاری** با سقف کل و سقف ساعتی (اولویت: تصمیم ورود > اسکن آزاد > بازتاب).
- تعویض مدل در حین اجرا: فایل `config/runtime.yaml` که هر چرخه خوانده می‌شود؛ تغییر `llm_profile` بدون ری‌استارت اعمال و لاگ می‌شود. زنجیره جایگزین فقط مدل‌های قوی.

**AI-3 — ارزیابی و اثبات «AI واقعی» برای داور**
- بهترین شاهد زنده برای ۳۵ امتیاز AI: یک **حساب دوقلوی کاغذی بدون LLM** روی همان کارت‌ها (هزینه صفر) ← AI lift زنده. به‌علاوه سهم تصمیم AI (سطح سایز، SL، TP، زمان را LLM تعیین می‌کند)، پوشش ۱۰۰٪ thesis/invalidation و حافظه بازتابی.
- پرامپت v2 قبل از اجرا با باقی‌مانده سهمیه CodeCraft (نه BoxAPI) روی ۳۰ کارت تست B آفلاین چک شود (P1-04): معیار موفقیت «نرخ ورود ۳۰–۶۰٪ و میانگین baseline کارت‌های پذیرفته > ردشده».

### ۴.۲ متخصصان بازارهای مالی

**MKT-1 — ریزساختار و هزینه اجرا**
- Market روی اکثر ارزها بسته ← خرید/فروش با **Limit قابل‌اجرا** که روی دفتر سفارش راه می‌رود (قیمت لازم برای پر شدن کل مقدار، با سقف لغزش). taker ۰٫۳۰٪ در عمل برای هر دو طرف.
- OCO نداریم: استاپ سمت سرور (STOP_LIMIT) مقدار را قفل می‌کند ← TP، خروج زمانی و بستن پایانی سمت کلاینت و همیشه **اول لغو استاپ، بعد فروش.**
- اسپرد PUMP ۰٫۴۶٪، ARB ۰٫۶۵٪، SOL ۰٫۲۵٪ ← TP زیر ۵٪ ارزش ندارد. بستن نهایی از ساعت ۲۲:۰۰ با لغزش پله‌ای؛ به «فروش آنی» برگزارکننده تکیه نکنیم.

**MKT-2 — استراتژی و آمار**
- قواعد ثابت لبه پایدار ندارند (پرتفوی ۲۵ روزه منفی، پنجره‌های ۲۴ ساعته میانه صفر). اما یک الگوی پایدار پیدا شد: **اولین سیگنال افت بد است، سیگنال‌های بعدی و افت‌های عمیق خوب‌اند** — و منطق «یک پوزیشن در هر ارز» دقیقاً اولی را می‌گیرد و جلوی بعدی‌ها را می‌بندد.
- خروج: SL ۵٪، TP ۶٪، نگهداری تا ۲۴ ساعت. استاپ ≤۳٪ با نوسان عادی خورده می‌شود؛ نگهداری ۴ ساعته منفی است ← ورودهای دیروقت (نزدیک پایان پنجره) فقط سایز آزمایشی.
- احتیاط: پارامترها را ریز تنظیم نکنیم؛ بازه به LLM بدهیم و فقط قواعد پایدار در هر دو نیمه را کد کنیم.

**MKT-3 — ریسک و پرتفوی**
- سقف ضرر هر معامله ۰٫۳٪ ارزش حساب (≈۱۵ هزار) با **فاصله مؤثر** (SL + لغزش استاپ ۱٫۵٪ + کارمزد ۰٫۶٪ ≈ ۷٪) ← سایز = min(سطح، ۱۵k ÷ ۰٫۰۷۱). بزرگ‌ترین پوزیشن ~۲۱۰ هزار (~۴٪ حساب).
- سقف گرمای پرتفوی Σ(سایز×SL) ≤ ۱٫۵٪ (۷۵ هزار)؛ حداکثر ۶ پوزیشن؛ میم‌ها حداکثر ۳؛ ۵۰٪ نقد.
- تنظیم‌کننده افت نرم از ارزش شروع: ‎−۲٪‎ ← سایز نصف؛ ‎−۴٪‎ ← فقط مدیریت پوزیشن‌ها؛ برگشت به زیر ‎−۳٪‎ ← ادامه.
- همه پوزیشن‌ها لانگ کریپتو و هم‌بسته‌اند ← فیلتر سقوط BTC (‎≤−۰٫۵٪‎ در ۱۵ دقیقه: ورود نه) و سقف شب (۰ تا ۶ صبح فقط تا «standard»). بدون نگهداری تتر (نوسان روزانه ۱٫۵٪ ریسک افت را بالا می‌برد).

### ۴.۳ متخصصان نرم‌افزار

**SWE-1 — معماری اجرای واقعی**
- با ۳ ساعت وقت، ساختن مدیر پوزیشن زنده جدید پرریسک است. پیشنهاد: **اجراکننده آینه‌ای**: موتور کاغذی همان تصمیم‌ها و خروج‌ها را می‌گیرد (همان کد بک‌تست و سایه)؛ `LiveExecutor` از طریق دو hook (`on_open` و `on_close`) هر باز و بسته شدن را روی والکس تکرار می‌کند و بعد از fill، قیمت و مقدار واقعی را به موتور برمی‌گرداند. اصل «یک موتور، سه حالت» حفظ می‌شود.
- `WallexClient` کوچک: markets، depth، balances، ساخت/خواندن/لغو سفارش با `client_id`، openOrders، trades؛ گرد کردن به stepSize/tickSize؛ retry فقط برای خواندن؛ برای ساخت سفارش بعد از timeout اول وضعیت با `client_id` پرسیده شود.

**SWE-2 — پایداری و تست**
- ذخیره وضعیت هر چرخه (پوزیشن‌ها، شمارنده‌ها، نگاشت زنده، `lo` شبکه زمانی) و بازیابی + آشتی با موجودی والکس بعد از ری‌استارت؛ ناظر (`run_live.py`) با ری‌استارت خودکار.
- تست آفلاین با `FakeWallexClient`، رگرسیون بیت‌به‌بیت کمبوهای قدیمی، بازپخش، اجرای خشک با داده زنده، و تست دود واقعی.

**SWE-3 — عملیات، مشاهده‌پذیری و تحویلی‌ها**
- ویندوز: Sleep خاموش، پنجره Anaconda جدا، UTF-8. وضعیت لحظه‌ای (`status.json` هر ۵ دقیقه؛ نسخه خوانای `status.md` در P1): ارزش واقعی از والکس، پوزیشن‌ها، سفارش‌ها، هزینه LLM.
- هر رکورد معامله `code_version` (hash گیت)، `prompt_version` و `llm_profile` دارد؛ قبل از شروع tag `live-v1`؛ هر ری‌استارت با کد جدید tag جدید. GitHub = نسخه‌ای که معامله کرده.

### ۴.۴ بحث اتاق مشترک: تعارض‌ها و جمع‌بندی

| موضوع | دیدگاه‌ها | جمع‌بندی |
|---|---|---|
| SL تنگ (۲–۳٪) یا باز (۵٪)؟ | ریسک (MKT-3): ضرر هر پوزیشن محدود؛ آمار (MKT-2): استاپ تنگ با نوسان خورده می‌شود | SL ۴٫۵–۵٪ + سقف ضرر با سایز (تأیید محسن) |
| فعالیت ۱۰–۱۵ در برابر لبه | AI-1: کیفیت؛ MKT-2: مسلح‌سازی فعالیت M1 را کم می‌کند | سطح سایز «probe» برای ستاپ متوسط + اسکن آزاد ۳۰ دقیقه‌ای (تأیید محسن) |
| اولین سیگنال: probe یا رد؟ | AI-3: probe فعالیت می‌دهد؛ MKT-2: probe روی اولین سیگنال جای سیگنال بهتر بعدی را می‌گیرد (یک پوزیشن در هر ارز) | در P0 رد با کد (بدون هزینه توکن)؛ «افزودن به پوزیشن» به عنوان کار آینده |
| استاپ سمت سرور یا کلاینت؟ | MKT-1: OCO نیست و مقدار قفل می‌شود؛ SWE-2: بدون استاپ سرور، کرش برنامه = پوزیشن بی‌دفاع | استاپ سرور + «لغو قبل از خروج»؛ اگر تست دود نشان داد پشتیبانی نمی‌شود: استاپ کلاینت هر ۱۵ ثانیه + ناظر |
| مدیر زنده جدید یا آینه؟ | SWE-1: آینه سریع و هم‌سان با بک‌تست؛ SWE-2: ریسک واگرایی کاغذ و واقعیت | آینه + همگام‌سازی بعد از هر fill + آشتی ۵ دقیقه‌ای + حذف پوزیشن کاغذی در صورت شکست خرید |
| CodeCraft باقی‌مانده؟ | کاربر: در اجرای واقعی نه | فقط برای چک آفلاین پرامپت v2 (P1-04) |
| ارکستراتور C6 و مدیر ریسک C3؟ | AI-2: فراخوانی اضافه؛ در سایه همه را خاموش کرد | حذف از C7؛ یک تصمیم‌گیرنده + گاردریل کد + حافظه بازتابی |
| جفت‌های تتری (کارمزد ۲٫۵ برابر ارزان‌تر)؟ | MKT-1: جذاب؛ MKT-3: ریسک نرخ تتر؛ SWE: حسابداری چندارزی و تأیید نشده | این اجرا نه؛ در «کارهای آینده» |
| ورود نزدیک پایان پنجره | MKT-2: نگهداری کوتاه منفی است؛ AI-3: فعالیت | بعد از جمعه ۱۴:۰۰ فقط probe؛ بعد از ۲۰:۰۰ ورود نه؛ بستن ۲۲:۰۰ تا ۲۲:۴۵ |

---

## ۵. طراحی نهایی ایجنت C7

### ۵.۱ جریان تصمیم (هر دقیقه؛ و تیک سریع هر ۱۵ ثانیه برای سفارش‌ها و استاپ‌ها)

```
کندل ۱ دقیقه بسته‌شده (LiveFeed) ─► features / regime / scanners (بدون تغییر)
   │
   ├─ کارت M1 روی ۹ ارز نقد ──► [کد] فیلتر سقوط BTC (btc15 ≤ −0.5% → رد)
   │                            [کد] مسلح‌سازی: اولین سیگنال در ۶ ساعت و افت از سقف ۶ساعته > −8% → «armed» (رد، بدون LLM)
   │                            [کد] precheck: ظرفیت، کول‌داون، تنظیم‌کننده افت، پنجره زمانی، pause
   │                            [LLM] decide_v2 → skip | enter + size_tier + SL/TP/زمان + thesis/invalidation
   │                            [کد] guard: سطح→سایز، سقف ریسک ۰٫۳٪، سقف گرما، ۵۰٪ نقد، کلمپ بازه‌ها، زمان تا پایان
   │
   ├─ هر ۳۰ دقیقه (اگر ظرفیت هست) ──► [LLM] native_v2 روی جدول ۹ ارز → حداکثر ۲ خرید → همان guard
   │
   ├─ موتور کاغذی: باز/بسته شدن پوزیشن ──► hook ──► LiveExecutor ──► والکس (Limit روی دفتر + STOP_LIMIT)
   │                                                   └─ fill واقعی → همگام‌سازی موتور
   ├─ تنظیم‌کننده افت (از ارزش واقعی والکس) · آشتی ۵ دقیقه‌ای · ذخیره وضعیت هر چرخه
   ├─ بعد از هر معامله بسته‌شده ──► [LLM] reflect → درس (حداکثر ۵ درس آخر در پرامپت‌ها)
   └─ جمعه ۲۲:۰۰ ──► بستن همه با لغزش پله‌ای؛ ۲۲:۴۵ ضرب‌الاجل؛ گزارش flatten
```

### ۵.۲ پارامترهای C7 (همه در `combos.py`؛ هر چه قابل تنظیم زنده است در `runtime.yaml`)

| پارامتر | مقدار | دلیل |
|---|---|---|
| ارزها | BTC, PEPE, PUMP, ETH, ARB, DOGE, SOL, S, BNB (TMN) | تنها ارزهای با نقدشوندگی قابل اتکا |
| اسکنر | فقط M1 (+ اسکن آزاد LLM) | تنها لبه پیش از هزینه |
| مسلح‌سازی | پنجره ۳۶۰ دقیقه؛ ماشه = سیگنال تکراری یا افت ≤ ‎−۸٪‎ از سقف ۶ ساعته | پایدار در هر دو نیمه |
| فیلتر BTC | BTCUSDT بازده ۱۵ دقیقه ≤ ‎−۰٫۵٪‎ ← ورود نه (M1 و native) | ‎−۱٫۴۴٪‎ هر معامله |
| سطح سایز | probe ۶۵k · standard ۱۵۰k · high ۲۲۰k تومان (بعد از سقف ریسک ~۲۱۱k) | تصمیم LLM |
| سقف ریسک هر معامله | ۰٫۳٪ ارزش حساب ÷ فاصله مؤثر (SL + ۱٫۵٪ + ۰٫۶٪) | تأیید محسن |
| SL / TP / زمان | SL ۴٫۵–۵٫۰ (پیش‌فرض ۵) · TP یک‌مرحله‌ای ۵–۷ (پیش‌فرض ۶) · تا ۱۴۴۰ دقیقه، محدود به زمان مانده تا ۲۲:۰۰ جمعه | شواهد |
| سقف‌ها | ۶ پوزیشن · درگیری ۵۰٪ · گرما ۱٫۵٪ · میم ≤ ۳ · ۳ ورود در ساعت · یک پوزیشن در هر ارز · کول‌داون ۲۴۰ دقیقه بعد از استاپ | ریسک |
| تنظیم‌کننده افت | ‎−۲٪‎ سایز ×۰٫۵ (کف ۶۵k) · ‎−۴٪‎ فقط مدیریت · برگشت زیر ‎−۳٪‎ | تأیید محسن |
| شب (۰۰ تا ۰۶) | حداکثر standard | ساعت ۰–۵ منفی |
| پنجره | جمعه بعد از ۱۴:۰۰ فقط probe · بعد از ۲۰:۰۰ ورود نه · ۲۲:۰۰ شروع بستن · ۲۲:۴۵ ضرب‌الاجل | نگهداری کوتاه منفی است |
| توقف روزانه قدیمی | خاموش برای C7 (۰٫۹۹) | جایگزین: تنظیم‌کننده افت |

### ۵.۳ نقش‌های LLM در C7

| نقش | کی | ورودی | خروجی | پرامپت |
|---|---|---|---|---|
| decide | هر کارت M1 که از کد رد شد | کارت + نمای ارز + ویژگی‌های شواهد + بازار + پرتفوی + درس‌ها | `skip/enter`, `size_tier`, `setup_quality`, SL, TP, زمان، thesis، invalidation | `prompts/decide_v2.md` |
| native | هر ۳۰ دقیقه، فقط اگر ظرفیت ورود هست | جدول ۹ ارز (+ مسلح‌ها) + بازار + پرتفوی | حداکثر ۲ خرید با همان فیلدها | `prompts/native_v2.md` |
| reflect | بعد از هر معامله بسته‌شده | خلاصه معامله | یک درس + تگ | `prompts/reflect_v1.md` (بدون تغییر) |

تعیین‌کننده نهایی همیشه گاردریل کد است؛ LLM فقط داخل بازه‌ها تصمیم می‌گیرد (برای داور: «AI تصمیم می‌گیرد، کد محافظت می‌کند»).

### ۵.۴ اجرای واقعی (آینه‌ای)

- **منبع حقیقت = والکس** (موجودی و سفارش‌ها). موتور کاغذی تصمیم و زمان خروج را می‌دهد؛ `LiveExecutor` اجرا می‌کند و اعداد واقعی را به موتور برمی‌گرداند.
- **ورود:** hook بعد از به‌روزرسانی کامل `pending` صدا زده می‌شود ← دفتر سفارش ← قیمت Limit لازم برای پر شدن کل مقدار (سقف ‎+۰٫۴٪‎ بالای بهترین ask) ← `LIMIT BUY` ← پایش تا ۱۵ ثانیه ← لغو باقی‌مانده ← مقدار واقعی از موجودی **کل** ← اگر ارزش پرشده بین صفر و ۵۵ هزار: خرید تکمیلی `max(هدف − پرشده، ۵۲ هزار)` ← همگام‌سازی پوزیشن کاغذی ← `STOP_LIMIT SELL` (stop = ورود×(۱−SL)، limit = stop×(۱−۱٫۵٪)) **فقط اگر** `stop_supported: true` (بولین). خرید ناموفق ← حذف پوزیشن کاغذی و برگشت نقد؛ دو رد پیاپی روی یک ارز ← ارز برای بقیه اجرا غیرفعال.
- **خروج (TP / زمان):** `on_close` ← لغو استاپ و تأیید ← `LIMIT SELL` روی bidها با نردبان لغزش `broker.yaml` ← حداکثر ۳ بار ← باقی‌مانده زیر ۵۰ هزار = خرده (لاگ).
- **محافظ مستقل:** thread جدای `fast_tick` هر ۱۵ ثانیه با قفل هر ارز (مشترک با hookها): وضعیت استاپ‌های سرور، استاپ کلاینت (اگر استاپ سرور نیست یا bid از limit آن گذشت)، و ثبت دوباره استاپی که در والکس باز نیست یا مقدارش با موجودی نمی‌خواند. هر بسته شدن خارج از موتور (`SERVER_STOP`، `CLIENT_STOP`) در صف «بستن کاغذی» می‌رود و `on_close` برای هر وضعیت `closed_*` فقط اعداد را وصله می‌کند (فروش دوم ممنوع).
- **بستن پایانی بر اساس موجودی (مستقل از موتور):** از جمعه ۲۲:۰۰ هر دقیقه: لغو همه سفارش‌های `c7-`، فروش هر ارز C7 با ارزش ≥ ۵۰ هزار با نردبان لغزش، تا ۲۲:۴۵. پوزیشن‌هایی که قبل از ربات در حساب بوده‌اند (snapshot شروع) لمس نمی‌شوند.
- **قطع یا کرش (Ctrl+C، exception):** هیچ فروشی انجام نمی‌شود؛ hookها جدا، وضعیت ذخیره، خروج. `close_all`/بستن فقط وقتی زمان به `flatten_start` رسیده.
- **ری‌استارت:** پوزیشن‌ها از `live_state.json` (با timestamp، نه اندیس) بازسازی و با موجودی و سفارش‌های باز والکس آشتی می‌شوند؛ سفارش‌های `c7-` دوباره وصل می‌شوند، لغو نمی‌شوند. ارز C7 با موجودی ≥ ۵۵ هزار که ربات خریده ولی در وضعیت نیست ← پذیرش با استاپ پیش‌فرض (bid×۰٫۹۵).

---

## ۶. تسک‌ها برای ایجنت (به ترتیب اجرا)

> قرارداد: هر تسک = بلوک YAML + «مشخصات» + «پذیرش». هیچ تسکی رفتار C1–C6 را عوض نمی‌کند (کلیدهای جدید فقط در C7 فعال‌اند؛ hookها پیش‌فرض `None`).

### P0 — لازم برای شروع اجرای واقعی

> دامنه P0 عمداً کوچک شده (بازبینی مستقل نشان داد نسخه کامل در زمان جا نمی‌شود). حساب دوقلو، حافظه بازتابی، تعویض داغ پرامپت، سقف ساعتی هزینه و `status.md` به P1 رفته‌اند. **تعویض داغ مدل در P0 می‌ماند** (درخواست صریح محسن).

#### P0-00 · آماده‌سازی و ایمنی
```yaml
id: P0-00
files: [docs/PROPOSAL_PROGRESS.md, .env.example]
depends_on: []
est_min: 5
owner: agent + Mohsen
```
- branch `live-c7`؛ `docs/PROPOSAL_PROGRESS.md` با جدول تسک‌ها (id، وضعیت، زمان، نتیجه تست).
- `python tests/test_fill_model.py` ← `15/15 passed`.
- **ایمنی گیت:** `git check-ignore .env .cache runs data` باید هر چهار مسیر را چاپ کند، وگرنه اول `.gitignore` را درست کن. از این به بعد فقط با مسیر صریح stage کن (`git add <files>`؛ هرگز `git add -A` یا `.`).
- `.env.example` ← `WALLEX_API_KEY=`، `BOXAPI_API_KEY=`، `CODECRAFT_API_KEY=`.
- **محسن:** کلید BoxAPI را خودش در `.env` بگذارد؛ ویندوز: `powercfg /change standby-timeout-ac 0` و `powercfg /change hibernate-timeout-ac 0`، عمل بستن درب لپ‌تاپ = «Do nothing»، Windows Update یک روز Pause، QuickEdit پنجره کنسول خاموش (کلیک روی کنسول برنامه را متوقف می‌کند).
- **پذیرش:** تست طلایی سبز؛ check-ignore درست؛ وجود `BOXAPI_API_KEY` در `.env` چک شود (مقدار چاپ نشود).

#### P0-01 · لایه LLM: پروفایل BoxAPI، دفتر هزینه دلاری جدا، زنجیره جایگزین
```yaml
id: P0-01
files: [config/llm.yaml, wallex_agent/llm/adapter.py, wallex_agent/llm_policy.py, tools/llm_check.py, tests/test_llm_budget.py]
depends_on: [P0-00]
est_min: 20
```
1. پروفایل‌ها و نقش‌ها طبق پیوست A1 (`decide`، `native`، `reflect` ← `[box_sol61, box_sol6, box_sol56]`). پروفایل‌های CodeCraft برای کار آفلاین می‌مانند.
2. `TokenLedger` ← `Ledger(name)` با یک نمونه برای هر نام (`ledger:` در پروفایل؛ پیش‌فرض `codecraft`). پوشه دفترها از env `LLM_LEDGER_DIR` (پیش‌فرض `.cache`)؛ `codecraft` ← همان `token_ledger.txt` قدیمی؛ بقیه ← `ledger_<name>.txt` با خطوط `tokens usd profile unix_ts role`. **تست‌ها حتماً با `LLM_LEDGER_DIR` موقت اجرا شوند** تا دفتر واقعی آلوده نشود. سقف توکن CodeCraft هرگز BoxAPI را نبندد.
3. هزینه دلاری از `usage` (معنای OpenAI): `cost = ((prompt − cached)·in + cached·cached_in + completion·out) / 1e6`؛ `cached = usage.prompt_tokens_details.cached_tokens` یا ۰. اگر `usage` نبود: تخمین طول/۳٫۵ + ۶۰۰ توکن خروجی.
4. سقف کل `usd_cap` (پیش‌فرض ۲٫۷، env `LLM_BUDGET_USD`). **اولویت:** `LLM.decide(..., priority)`؛ `native` و `reflect` (`normal`/`low`) در ۸۰٪ سقف قطع می‌شوند؛ `decide` (`high`) تا ۱۰۰٪. فراخوان‌ها: `LLMPolicy.decide` ← `high`، `Native.pick` ← `normal`، `Reflector.reflect` ← `low`.
5. `LLMResult.cost_usd`؛ `log_call` و `LLMPolicy._log` فیلدهای `cost_usd`، `usage`، `code_version`، `prompt_version` را بنویسند.
6. کش برای هر پروفایل قابل خاموش شدن (`cache: false` در پروفایل‌های box)؛ پیش‌فرض کلی `true` بماند.
7. override تک‌اسمی ← `[override] + اعضای زنجیره نقش با ledger یکسان`؛ لیست با کاما ← همان لیست.
8. `max_completion_tokens` برای پروفایل‌های box = `max_output_tokens + 2500`.
9. `tools/llm_check.py`: هزینه هر فراخوانی و جمع دفتر را چاپ کند.

**پذیرش:** `python tools/llm_check.py --llm box_sol61 --n 2` ← `READY`، تأخیر < ۳۰ ثانیه، هزینه < ۰٫۰۲ دلار. **محسن** بلافاصله عدد مصرف را با داشبورد BoxAPI مقایسه کند؛ اگر بیشتر بود، `price_usd_per_m` را اصلاح کن. اگر 403/Cloudflare: `user_agent` پروفایل (`opencode/1.19.3`، بدون UA، UA مرورگر). T2 سبز.

#### P0-02 · فایل کنترل زمان اجرا و تعویض داغ مدل
```yaml
id: P0-02
files: [config/runtime.yaml, wallex_agent/runtime.py, wallex_agent/llm_policy.py, tests/test_runtime_control.py]
depends_on: [P0-01]
est_min: 10
```
- `config/runtime.yaml` طبق پیوست A2. `Control.load()` با کش mtime؛ اعتبارسنجی (`size_mult` ۰–۱٫۵، `native_every_min` ۱۵–۲۴۰، زمان‌ها `YYYY-MM-DD HH:MM` تهران با آفست ثابت ‎+03:30‎ — بدون `zoneinfo`)؛ نامعتبر ← آخرین نسخه معتبر + هشدار؛ هر تغییر ← `runs/<run>/control_changes.jsonl`.
- `LLMPolicy.set_profile(s)`، `Native.set_profile(s)`، `Reflector.set_profile(s)`: فقط با تغییر رشته LLM از نو ساخته شود.
- فایل کنترل فقط پارامتر عوض می‌کند؛ هیچ مسیر سفارش دستی نسازید.
- **پذیرش:** T3 سبز.

#### P0-03 · تعریف کمبوی C7
```yaml
id: P0-03
files: [wallex_agent/combos.py]
depends_on: []
est_min: 5
```
- دیکشنری `C7` طبق پیوست A3 (با صفرهای صریح `be_pct`، `trail_atr`، `time_stop_min_profit`) + `PROFILE_SCANNERS["C7"] = {"M1"}`.
- **پذیرش:** `python -c "from wallex_agent.combos import COMBOS; c=COMBOS['C7']; print(c['coins'], c['size_tiers'])"`.

#### P0-04 · گاردریل‌های موتور برای C7
```yaml
id: P0-04
files: [wallex_agent/engine.py, tests/test_c7_guardrails.py]
depends_on: [P0-03]
est_min: 30
```
همه فقط وقتی کلید مربوط در کانفیگ هست (C1–C6 بدون تغییر):
1. فیلدهای جدید `Account.__init__`: `window=None`، `control=None`، `hooks=None`، `equity_fn=None`، `flatten_mode=False`، `gov_state="normal"`، `m1_hist=defaultdict(set)` (مجموعه اندیس بار هر ارز ← حذف تکرار)، `disabled_coins=set()`. **`start_cash` را runner از ارزش واقعی والکس در اولین شروع می‌دهد** (۵M) و در `live_state.json` ذخیره می‌شود؛ پیش‌فرض ۲٫۵M کلاس نباید در C7 استفاده شود.
2. **مسلح‌سازی** در `on_card` (C7، اسکنر M1)، قبل از هر رد دیگر: اندیس کارت را در `m1_hist[sym]` اضافه کن؛ `prev` = تعداد اندیس‌های قبلی در `arm_window_min`؛ `dd6 = c[j] / max(h[j−360..j]) − 1`؛ اگر `prev == 0` و `dd6·100 > arm_dd_pct` ← رد `armed_first_signal`. مقدارها در `card.info["m1_prev_6h"]` و `card.info["dd6_pct"]`.
3. افزوده‌های `precheck`: `coin_disabled`، `btc15_block` (آخرین کندل ۱۵ دقیقه BTCUSDT ≤ آستانه ← `btc_falling`)، `control.pause_entries` ← `paused_by_control`، `flatten_mode`، زمان ≥ `no_new_entries_after_ts` ← `window_closed`، `gov_state=="hard"` ← `dd_governor`، `control.max_positions` به جای `max_pos`.
4. افزوده‌های `guard` (وقتی `size_tiers` هست):
   - `tier = dec.get("size_tier") or "standard"`؛ شب (`night_hours`) ← حداکثر `night_tier_cap`؛ بعد از `probe_only_after_ts` ← `probe`.
   - `size = size_tiers[tier] × control.size_mult`؛ در `gov_state=="soft"` ← `× 0.5`؛ هر دو ضریب **با کف ۶۵ هزار** (probe کوچک‌تر از حداقل نمی‌شود)؛ `hard` ← ورود نه.
   - **سقف ریسک با فاصله مؤثر:** `eff = sl/100 + stop_limit_offset + 0.006` (لغزش استاپ + کارمزد دو طرف)؛ `size ≤ risk_cap_pct × equity ÷ eff` (با SL ۵٪: eff=۷٫۱٪ ← حداکثر ~۲۱۱ هزار در ۵M). سپس فقط سقف بالای `size_band`؛ اگر `< 65,000` ← رد `size_below_min`.
   - گرما: `Σ(qty·entry_px·eff) + size·eff ≤ heat_cap_pct × equity` وگرنه `heat_cap`.
   - TP تک‌مرحله‌ای `clamp(dec.tp_plan[0].pct, tp_band)`، `portion=1.0`؛ Plan بدون BE و trailing.
   - زمان: `ts = min(dec.time_stop_min or 1440, cfg.time_stop, (flatten_start_ts − now)/60 − 5)`؛ اگر `ts < 60` ← رد `too_close_to_end`.
   - در رکورد تصمیم: `tier_requested`، `tier_final`، `size`، `risk_tmn`.
5. **تنظیم‌کننده افت** در `on_bar`: `dd = 1 − equity/start_cash` (`equity_fn()` اگر ست شده؛ همین منبع برای guard و گرما)؛ گذارها: ≥ `dd_soft` ← `soft`؛ ≥ `dd_hard` ← `hard`؛ `hard` و `dd < dd_resume` ← `soft`؛ `soft` و `dd < 0.01` ← `normal`؛ هر گذار ← رکورد `governor`.
6. **بستن:** اگر `flatten_mode`: همان مسیر `close_all` (`exit_market(f, j, "END")` + `_book_exits`).
7. **hookها — ترتیب حیاتی:** در `_fill_pending` پوزیشن‌های باز‌شده را در یک لیست جمع کن و **بعد از** `self.pending = keep` و `entries_hist.append` برای هر کدام `hooks.on_open(self, pos, j)` را صدا بزن. در `_close` hook آخرین خط است. هر دو داخل `try/except`: خطا ← لاگ `hook_error` + `executor.mark_unknown(sym)` (آشتی بعدی حلش می‌کند)؛ هرگز دوباره raise نشود. `equity_fn` هم داخل try با fallback به ارزش کاغذی.
8. متدهای کمکی: `cancel_position(sym_idx, reason, j)` (حذف پوزیشن، برگرداندن `entry_px·qty+fees` به نقد، حذف همان `j` از `entries_hist` اگر هست، رکورد `live_entry_failed`)؛ `sync_position(sym_idx, entry_px, qty, fees)`؛ `adopt_position(sym_idx, qty, px, j, plan)`.
9. `context()` برای C7: `m1_prev_6h`، `dd6_pct`، `tehran_hour`، `hours_to_flatten`، `trades_24h`، `gov_state`.
10. `daily_stop` برای C7 = ۰٫۹۹.

**پذیرش:** T4 سبز + T1 همچنان ۱۵/۱۵.

#### P0-05 · پرامپت v2 و زمینه کالیبره
```yaml
id: P0-05
files: [prompts/decide_v2.md, prompts/native_v2.md, wallex_agent/llm_policy.py, wallex_agent/policies.py]
depends_on: [P0-02, P0-04]
est_min: 20
```
1. پرامپت‌ها از پیوست B (محتوای جدول شواهد عوض نشود).
2. **نسخه برای هر پرامپت جدا:** `decide: v2`، `native: v2`، `reflect: v1` (فایل `reflect_v2` وجود ندارد). `_p(name, version)`. v1 دست‌نخورده بماند.
3. `DECISION_SCHEMA`: `size_tier` و `setup_quality` اختیاری‌اند؛ `size_tier` فقط وقتی `action=="enter"` اعتبارسنجی شود (پاسخ skip با `null` نباید رد شود). نبودش در enter ← `standard`.
4. `ContextBuilder.card` در v2: `natural_target_pct_above` ← `nearby_resistance_pct`؛ بلوک `evidence_features = {liquidity_rank_pct, m1_prev_6h, dd_from_6h_high_pct, btc_above_1h_ema50, btc_ret_15m_pct, tehran_hour, hours_to_flatten, coin_ret_24h_pct}`؛ `portfolio` = ارزش، پوزیشن‌ها (ارز، سود٪)، `trades_24h`، `gov_state`؛ `contest = {flatten_start, hours_left, target_trades_24h: "10-15"}`. `liquidity_rank_pct` = صدک `share_of_minutes_traded` پانزده‌دقیقه‌ای بین ۲۸ ارز هسته.
5. `Native.pick` v2: همان جدول فعلی (`coin_view`) + `dd_from_6h_high`، `m1_signals_6h`، `liquidity_rank_pct`، `held`؛ schema پیوست C.
6. اندازه: پرامپت سیستم ≤ ۱,۰۰۰ توکن تخمینی (طول/۳٫۵)؛ payload تصمیم ≤ ۱,۲۰۰ و native ≤ ۱,۸۰۰.
**پذیرش:** T5 سبز؛ نمونه payload در `runs/test_prompt_v2/sample_payload.json`.

#### P0-06 · اجرای C7 در حلقه سایه/زنده
```yaml
id: P0-06
files: [wallex_agent/shadow.py]
depends_on: [P0-04, P0-05]
est_min: 20
```
1. آرگومان‌ها: `--accounts C7`، `--live`، `--end "YYYY-MM-DD HH:MM"` (تهران؛ بر `--until` مقدم)، `--resume`، `--control config/runtime.yaml`، `--fast-tick 15`، `--native-at-start`، `--flatten-start-in N` (فقط اجرای خشک: تمرین بستن).
2. `make_accounts` برای C7: `Account("C7", LLMPolicy("C7", chain, log_path=llm_C7.jsonl), ..., start_cash=<ارزش واقعی یا ذخیره‌شده>)` + `Native` و `Reflector`؛ `window` و `control` از کنترل.
3. `step_account` C7: کارت‌های M1 ← `on_card`. اسکن آزاد **بر اساس زمان گذشته** (`now − last_native ≥ native_every_min`، نه مرز دقیقه) و فقط اگر ظرفیت پوزیشن، نبودِ pause/flatten/hard، قبل از `no_new_entries_after`، ذخیره نقد و BTC سالم ← `_direct(acc, card, dec, j, profile="C7")` (پارامتر `profile` اضافه شود).
4. بازتاب بعد از هر معامله بسته‌شده (اولویت `low`)؛ درس‌ها فقط در حافظه (ذخیره در P1).
5. شروع و resume: کارت‌های M1 ارزهای C7 با `j0 − 360 ≤ idx < j0` در `m1_hist` (مجموعه ← بدون تکرار).
6. هر چرخه: `Control.load()` ← اعمال؛ `now ≥ flatten_start` یا `flatten_now` ← `flatten_mode=True` و `executor.start_flatten_loop()`.
7. در `now ≥ end` ← `executor.final_flatten()` و خروج عادی. **در `KeyboardInterrupt` یا exception حلقه اصلی:** `acc.hooks=None`، ذخیره وضعیت، خروج — بدون `close_all` و بدون فروش.
8. heartbeat: ارزش واقعی، حالت تنظیم‌کننده، هزینه و تعداد فراخوانی LLM، سفارش‌های باز.
**پذیرش:** T7 سبز.

#### P0-07 · کلاینت والکس و اجراکننده آینه‌ای
```yaml
id: P0-07
files: [wallex_agent/live/__init__.py, wallex_agent/live/wallex_client.py, wallex_agent/live/executor.py, tests/test_live_executor.py]
depends_on: [P0-04 (فقط رابط hook)]
est_min: 45
```
**`WallexClient`** (stdlib): پایه `https://api.wallex.ir`؛ هدر `x-api-key`؛ timeout ده ثانیه.
- `markets()` (دقت مقدار/قیمت، `minNotional`، `isMarketTypeEnable`، bid/ask/last)، `depth(sym)` ← `{"ask": [[px, qty]...], "bid": [...]}`، `balances()` ← `{asset: (total, free, locked)}` بر اساس `balance_value_includes_locked` در `broker.yaml` (اگر نامعلوم: `total = value + locked` محافظه‌کارانه و هشدار)، `open_orders(sym=None)`، `get_order(client_id)`، `account_trades(sym)`.
- `create_order(symbol, side, type, quantity, price=None, stop_price=None, client_id)` ← `POST /v1/account/orders`؛ نام فیلد استاپ از `broker.yaml`. `success=false`/4xx ← `OrderRejected(msg)`.
- `cancel_order(client_id)` با روشی که تست دود تأیید کرده (`query` / `body` / `path`) و تأیید با `get_order`.
- retry فقط GET (۰٫۵، ۱، ۲ ثانیه). ساخت سفارش بعد از timeout: `get_order(client_id)`؛ اگر نبود و `dup_id_rejected: true` ← یک بار دیگر با همان id؛ وگرنه **خرید دوباره ارسال نشود** (شکست؛ آشتی بعدی).
- قالب اعداد: مقدار رو به پایین به پله؛ قیمت خرید رو به بالا و فروش رو به پایین به tick؛ بدون نماد علمی. Limitهای ورود و خروج حداکثر ۳٪ از بهترین bid/ask؛ استاپ از این قاعده مستثنی ولی مشروط به `limit_rel_ok`.
- `client_id = c7-{epoch}-{sym}-{purpose}-{seq}`؛ `epoch` = base36 زمان شروع فرآیند؛ قبل از ارسال در وضعیت ثبت شود؛ طول آن در تست دود تأیید شده باشد.
- لاگ هر درخواست/پاسخ (بدون کلید) ← `orders.jsonl`؛ نوشتن فایل‌ها با retry (فایل باز در Excel نباید ربات را بخواباند).

**`LiveExecutor(account, client, cfg, run_dir)`** — رفتار §۵.۴. نکات اجباری:
- قفل هر ارز (`threading.Lock`) مشترک بین hookها، `fast_tick` و حلقه بستن.
- `live[sym] = {qty, avg_px, fees, entry_cid, stop_cid, stop_px, stop_limit_px, server_stop, sl_pct, tp_pct, time_stop_deadline_ts, size_tier, opened_ts, status}`؛ ذخیره در `live_state.json` بعد از هر تغییر (به‌همراه `start_cash`، `start_ts`، `epoch`، `seq`، `cooldowns` و `entries_hist` به timestamp، `gov_state`، `disabled_coins`، `initial_balances`).
- `fast_tick()` در thread جدا (هر ۱۵ ثانیه): (۱) استاپ‌های سرور با یک `open_orders` ← FILLED ← `closed_by_server` + صف بستن کاغذی؛ باز نبودن یا مقدار نادرست ← ثبت دوباره؛ (۲) استاپ کلاینت ← `exit` + `closed_by_client` + صف بستن کاغذی؛ (۳) وضعیت ورودهای در جریان.
- `reconcile()` (شروع و هر ۵ دقیقه): موجودی‌های اولیه (`initial_balances`) کنار گذاشته شوند؛ سفارش‌های باز `c7-` دوباره وصل؛ سفارش ناشناخته غیر `c7-` فقط لاگ؛ پوزیشن ثبت‌شده بدون موجودی (کل) ← بسته `RECONCILE_GONE`؛ موجودی C7 خریده‌شده توسط ربات ولی ثبت‌نشده ← `adopt` با استاپ پیش‌فرض.
- `start_flatten_loop()` و `final_flatten()`: §۵.۴ بستن پایانی بر اساس موجودی، با نردبان لغزش واحد `broker.yaml`؛ `flatten_report.json`.
- `live_equity()`: Σ موجودی **کل** × bid + TMN کل؛ به `account.equity_fn`.

**پذیرش:** T6 سبز.

#### P0-08 · ادامه بعد از ری‌استارت و ناظر
```yaml
id: P0-08
files: [wallex_agent/shadow.py, wallex_agent/live/executor.py, run_live.py, tests/test_resume.py]
depends_on: [P0-06, P0-07]
est_min: 15
```
- **بدون pickle:** وضعیت فقط `live_state.json` (P0-07) با timestamp و اسم نماد. در `--resume`: پوزیشن‌های کاغذی با `adopt_position` از روی timestamp ← اندیس شبکه جدید ساخته می‌شوند؛ شمارنده‌ها از timestamp؛ سپس آشتی سه‌طرفه (والکس منبع حقیقت).
- `run_live.py`: (۱) **همیشه `--live` می‌فرستد**؛ (۲) اگر `live_state.json` همان run وجود دارد، **همیشه `--resume`**؛ (۳) شروع نمی‌کند مگر `config/broker.yaml` مقدار بولین `stop_supported` داشته باشد و فایلی در `runs/smoke/` باشد؛ (۴) قبل از هر شروع `python -m wallex_agent.data.fetch --days 2`؛ (۵) خروجی فرزند به `child.log` (نه کنسول)؛ (۶) ری‌استارت روی خروج غیرصفر تا ۳۰ بار با ۱۰ ثانیه تأخیر تا ضرب‌الاجل؛ `supervisor.log`.
**پذیرش:** T8 سبز.

#### P0-09 · تست دود واقعی (بلافاصله بعد از P0-07؛ با تأیید محسن در همان لحظه)
```yaml
id: P0-09
files: [tools/live_smoke.py, config/broker.yaml]
depends_on: [P0-07]
est_min: 15
cost_tmn: "~800-1200"
```
- بخش ۱ (کلاینت خام): `python tools/live_smoke.py --symbol PEPETMN --tmn 60000 --yes` طبق پیوست D.
- بخش ۲ (اجراکننده): `python tools/live_smoke.py --executor --symbol PEPETMN --tmn 65000 --yes` ← `enter` → `place_stop` → `fast_tick` → `exit` با یک حساب ساختگی کوچک؛ روش‌های لغو `query`، `body`، `path` امتحان شوند.
- خروجی: `runs/smoke/<ts>.json` و `config/broker.yaml` (پیوست A4) با مقادیر **بولین**.
- **پذیرش:** خرید و فروش پر شدند؛ لغو کار کرد؛ رفتار استاپ قطعی شد؛ هیچ پوزیشن یا سفارش باز نمانده.

#### P0-10 · لاگ، نسخه، وضعیت لحظه‌ای
```yaml
id: P0-10
files: [wallex_agent/shadow.py, wallex_agent/live/executor.py]
depends_on: [P0-06, P0-07]
est_min: 10
```
- `code_version` (`git rev-parse --short HEAD` + `-dirty`) در `live_state.json`، decisions، لاگ LLM، orders و `trades_live.csv`؛ به‌علاوه `prompt_version` و `llm_profile`.
- `status.json` هر ۵ دقیقه.
- **پذیرش:** در اجرای خشک `status.json` این کلیدها را دارد: `equity_live`، `start_cash`، `dd_pct`، `gov_state`، `positions[]` (sym، qty، avg_px، last، pnl_pct، stop_px، server_stop)، `open_orders`، `trades` (n، wins، pnl_tmn)، `llm` (calls، cost_usd)، `errors_last`.

#### P0-11 · اجرای خشک و شروع
```yaml
id: P0-11
depends_on: [P0-01, P0-02, P0-03, P0-04, P0-05, P0-06, P0-07, P0-08, P0-09, P0-10]
est_min: 20
```
- اجرای خشک ۱۰ دقیقه (کاغذی؛ داده و LLM واقعی؛ تمرین بستن): `python -m wallex_agent.shadow --accounts C7 --llm box_sol61,box_sol6,box_sol56 --end "2026-10-22 23:59" --native-at-start --flatten-start-in 8 --run-id dry_c7` ← بعد از دقیقه ۸ هیچ ورود جدید و همه پوزیشن‌های کاغذی بسته.
- چک‌لیست Go/No-Go (§۸.۳) ← commit با مسیرهای صریح ← `git tag live-v1` و push ← شروع: `python run_live.py --run-id live_20261022 --accounts C7 --llm box_sol61,box_sol6,box_sol56 --end "2026-10-23 22:50"`

### P1 — بعد از شروع (با ری‌استارت کنترل‌شده؛ هر کدام commit و tag جدا)

| id | کار | فایل | زمان | پذیرش |
|---|---|---|---|---|
| P1-01 | حساب دوقلوی کاغذی `C7_baseline` = `Account("C7", BaselinePolicy("C7", COMBOS["C7"]), start_cash=همان)` با پوشه لاگ جدا؛ AI lift زنده در `status.json` | `shadow.py` | ۱۵ | خط AI lift در وضعیت |
| P1-02 | ذخیره درس‌های بازتاب در `live_state.json` و بازیابی بعد از resume | `shadow.py` | ۱۰ | بعد از ری‌استارت درس‌ها در payload |
| P1-03 | `status.md` خوانا + گزارش HTML اجرای واقعی از `trades_live.csv`، `orders.jsonl` و لاگ LLM | `report.py` | ۲۵ | `runs/<run>/report/index.html` |
| P1-04 | چک آفلاین پرامپت v2 با باقی‌مانده CodeCraft روی ۳۰ کارت تست B (`eval_cards.py --llm sol56 --prompt v2`)؛ نه BoxAPI | `eval_cards.py` | ۲۰ | نرخ ورود ۳۰–۶۰٪ و میانگین baseline پذیرفته‌ها > ردشده‌ها؛ اگر نرخ ورود < ۱۵٪ آستانه probe ۴۰←۳۵، اگر > ۸۰٪ ۴۰←۴۵ |
| P1-05 | رگرسیون بیت‌به‌بیت C1–C6 با بازه ثابت (`--end-ts` در backtest) قبل/بعد تغییرات | `backtest.py` | ۱۵ | sha256 یکسان `trades.csv` |
| P1-06 | تعویض داغ نسخه پرامپت و سقف ساعتی هزینه | `runtime.py`, `adapter.py` | ۱۰ | T3 گسترش‌یافته |

### P2 — در حین اجرا یا بعد از آن

| id | کار |
|---|---|
| P2-01 | `docs/SUBMISSION.md` برای داور: معماری، نقش AI، شرط خرید/فروش، مدیریت ریسک، اجرای مجدد، شواهد سه تست + اجرای واقعی؛ پر کردن `FINAL-SELECTION` در `PROJECT_REPORT.md` |
| P2-02 | `requirements.txt` (numpy، pandas، pyyaml؛ Python ≥ 3.10) و دستورهای اجرا در README |
| P2-03 | سناریوی ویدئوی ۵ دقیقه‌ای (پیوست F) |
| P2-04 | آینده: «افزودن به پوزیشن» (probe ← standard روی سیگنال تکراری) با چند lot در هر ارز |
| P2-05 | آینده: مطالعه جفت‌های تتری (کارمزد ۲٫۵ برابر کمتر) با حسابداری چندارزی |

---

## ۷. تست‌ها و دروازه‌های کیفیت

همه تست‌های آفلاین هم با `python tests/<file>.py` و هم با pytest اجرا شوند؛ تست‌هایی که دفتر هزینه می‌نویسند با `LLM_LEDGER_DIR` موقت.

| کد | تست | دستور | معیار عبور |
|---|---|---|---|
| T1 | شبیه‌ساز (موجود) | `python tests/test_fill_model.py` | ۱۵/۱۵ |
| T2 | بودجه LLM | `python tests/test_llm_budget.py` | فرمول هزینه با usage نمونه دقیق؛ پر بودن سقف CodeCraft BoxAPI را نمی‌بندد؛ در ۸۰٪ سقف دلاری native/reflect بسته و decide باز؛ در ۱۰۰٪ همه بسته؛ backend خراب ← پروفایل بعدی؛ فایل‌ها فقط در پوشه موقت |
| T3 | کنترل زمان اجرا | `python tests/test_runtime_control.py` | تغییر فایل در `load()` بعدی دیده می‌شود؛ YAML خراب ← مقدار قبلی؛ تغییر `llm_profile` ← زنجیره جدید؛ هر تغییر در `control_changes.jsonl` |
| T4 | گاردریل C7 | `python tests/test_c7_guardrails.py` | (۱) اولین M1 ← `armed_first_signal`؛ دومی در ۶ ساعت ← به policy می‌رسد؛ کارت تکراری در همان بار دو بار شمرده نمی‌شود؛ (۲) افت ≤ ‎−۸٪‎ بدون سیگنال قبلی ← به policy می‌رسد؛ (۳) BTC ‎−۰٫۶٪‎ ← `btc_falling`؛ (۴) high با SL ۵٪ و ارزش ۵M ← ~۲۱۱k (سقف مؤثر)، با `start_cash` و ارزش ۴٫۰M ← ~۱۶۹k؛ (۵) شب ← حداکثر standard؛ بعد از `probe_only_after` ← ۶۵k؛ (۶) گرما ← `heat_cap`؛ (۷) افت ۲٪ ← standard ۱۵۰k←۷۵k و probe در کف ۶۵k؛ افت ۴٪ ← `dd_governor`؛ برگشت زیر ۳٪ ← soft؛ (۸) زمان تا پایان < ۶۵ دقیقه ← `too_close_to_end`؛ (۹) Plan در C7 بدون BE و trailing؛ (۱۰) `coin_disabled`؛ (۱۱) hook که exception می‌دهد ← بار بعد سفارش دوم **ثبت نمی‌شود** (pending خالی)؛ (۱۲) C1–C6 روی کارت نمونه خروجی قبلی |
| T5 | پرامپت v2 | `python tests/test_prompt_v2.py` | پرامپت سیستم ≤ ۱۰۰۰ و payload تصمیم ≤ ۱۲۰۰ و native ≤ ۱۸۰۰ توکن تخمینی؛ `evidence_features` حاضر؛ `natural_target_pct_above` در v2 نیست؛ skip با `size_tier: null` معتبر؛ reflect همچنان v1 |
| T6 | اجراکننده آینه‌ای | `python tests/test_live_executor.py` | با `FakeWallexClient` که minNotional را روی **همه** سفارش‌ها (از جمله استاپ) اجرا می‌کند و موجودی قفل‌شده را مثل والکس گزارش می‌دهد: (۱) خرید کامل ← استاپ، هم‌گامی کاغذی؛ (۲) پر شدن جزئی ۳۰k ← خرید تکمیلی ۵۲k؛ (۳) رد خرید ← پوزیشن کاغذی حذف، نقد برگشت، دو رد ← ارز غیرفعال؛ (۴) خروج ← لغو استاپ بعد فروش؛ (۵) استاپ سرور FILLED ← بستن کاغذی `SERVER_STOP` بدون فروش دوم؛ (۶) استاپ کلاینت ← بستن کاغذی `CLIENT_STOP` بدون فروش دوم؛ (۷) **کل مقدار در استاپ قفل است ← آشتی پوزیشن را نمی‌بندد** و ارزش حساب درست است؛ (۸) استاپ ناپدیدشده ← ثبت دوباره؛ (۹) موجودی اولیه حساب ← adopt نمی‌شود؛ (۱۰) بستن پایانی بر اساس موجودی با دفتر کم‌عمق ← نردبان لغزش و گزارش؛ (۱۱) گرد کردن؛ (۱۲) timeout ساخت سفارش ← `get_order` و عدم ارسال دوباره خرید وقتی `dup_id_rejected` نامعلوم است؛ (۱۳) **قطع (KeyboardInterrupt) با پوزیشن باز ← صفر سفارش فروش** |
| T7 | بازپخش C7 | `python -m wallex_agent.shadow --replay-hours 6 --accounts C7 --llm mock --run-id test_c7_replay` | صفر خطای چرخه؛ رکوردهای `armed_first_signal` و (با mock که حداقل یک کارت را enter می‌کند) `accepted`؛ اسکن آزاد فقط با ظرفیت؛ heartbeat سالم |
| T8 | ادامه بعد از ری‌استارت | `python tests/test_resume.py` | `live_state.json` ← resume روی شبکه زمانی جدید: پوزیشن‌ها، استاپ‌ها و شمارنده‌ها با timestamp درست؛ سفارش‌های `c7-` دوباره وصل نه لغو؛ بازپخش: قطع بعد از ۲ چرخه، resume، بدون پوزیشن تکراری |
| T9 | LLM واقعی | `python tools/llm_check.py --llm box_sol61 --n 2` | READY، تأخیر < ۳۰ ثانیه، هزینه < ۰٫۰۲ دلار، هم‌خوان با داشبورد BoxAPI |
| T10 | تست دود واقعی | P0-09 | پیوست D |
| T11 | اجرای خشک | P0-11 | ۱۰ دقیقه، صفر خطای چرخه، هزینه LLM در لاگ، `status.json` کامل، تمرین بستن موفق |
| T12 | رگرسیون (P1) | P1-05 | sha256 یکسان |

---

## ۸. زمان‌بندی، خط برش و Go/No-Go

### ۸.۱ زمان‌بندی (پنجشنبه ۳۰ مهر، تهران)

جمع تخمین‌های P0 پشت‌سرهم ~۲۱۵ دقیقه است؛ با دو جریان موازی حدود ۱۵۰ دقیقه. جریان B از ابتدا با `FakeWallexClient` و رابط hookهای P0-04 (بند ۷ و ۸) کار می‌کند. **فایل‌های مشترک:** `engine.py` فقط در جریان A ویرایش می‌شود؛ جریان B فقط `wallex_agent/live/*`، `tools/live_smoke.py`، `run_live.py` و در P0-08/P0-10 بخش‌های مشخص `shadow.py` را بعد از اتمام P0-06 تغییر می‌دهد.

| ساعت | جریان A (موتور و AI) | جریان B (اجرای واقعی) | محسن |
|---|---|---|---|
| ۲۰:۵۰ | P0-00 | — | کلید BoxAPI در `.env`؛ تنظیمات ویندوز |
| ۲۰:۵۵ | P0-01، P0-02 ← T2، T3، T9 | P0-07 ← T6 | مقایسه هزینه با داشبورد BoxAPI |
| ۲۱:۳۰ | P0-03، P0-04 ← T4 | P0-07 ادامه | — |
| ۲۱:۵۰ | P0-05 ← T5 | **P0-09 تست دود** ← T10 | **تأیید سفارش واقعی** |
| ۲۲:۱۰ | P0-06 ← T7 | P0-07 اصلاح بر اساس `broker.yaml` | (خط برش اول) |
| ۲۲:۳۰ | یکپارچه‌سازی | P0-08، P0-10 ← T8 | — |
| ۲۲:۴۵ | T1–T8 کامل؛ P0-11 اجرای خشک ← T11 | | — |
| **۲۳:۰۰ ± ۱۰** | **Go/No-Go و شروع اجرای واقعی** | | **تصمیم Go** |
| بعد از شروع | P1 با ری‌استارت کنترل‌شده؛ P2 بدون توقف ربات | | پایش `status.json` |
| جمعه ۱۴:۰۰ / ۲۰:۰۰ / ۲۲:۰۰ / ۲۲:۴۵ | فقط probe / پایان ورود / شروع بستن / ضرب‌الاجل (خودکار) | | — |
| جمعه ۲۲:۵۰ | | | تأیید صفر بودن پوزیشن در اپ والکس، جمع‌آوری لاگ، commit و tag `final` |

### ۸.۲ خط برش (اگر عقب افتادیم)

**MVL (نسخه حداقلی) همیشه این سه را دارد:** آشتی و adopt هنگام شروع، استاپ کلاینت در thread جدا، و بستن پایانی بر اساس موجودی. بدون این سه شروع نکنید.

| ساعت | اگر آماده نیست | اقدام |
|---|---|---|
| ۲۲:۱۵ | STOP_LIMIT در تست دود تأیید نشد یا P0-07 کامل نیست | `stop_supported: false` (استاپ فقط کلاینت هر ۱۵ ثانیه) + MVL |
| ۲۲:۴۵ | `--resume` کامل نیست | شروع کن؛ ری‌استارت = شروع تازه با آشتی و adopt از موجودی (استاپ پیش‌فرض bid×۰٫۹۵، TP ‎+۶٪‎، زمان تا بستن) |
| ۲۲:۴۵ | BoxAPI آماده نیست | **تصمیم محسن:** (الف) شروع موقت با CodeCraft (`sol56`، ~۲۵۰ هزار توکن) و تعویض داغ به BoxAPI بعد از رفع، یا (ب) صبر |
| ۲۳:۱۵ | هنوز شروع نشده | شروع با MVL الزامی است؛ هر ساعت تأخیر ≈ ۴٪ پنجره |

### ۸.۳ چک‌لیست Go/No-Go

- [ ] T1–T8 سبز (`docs/PROPOSAL_PROGRESS.md`)
- [ ] T9: `box_sol61` آماده و هزینه با داشبورد BoxAPI هم‌خوان
- [ ] T10: تست دود PASS؛ `config/broker.yaml` با مقادیر بولین (`stop_supported`، `balance_value_includes_locked`، `limit_rel_ok`، `dup_id_rejected`)
- [ ] T11: اجرای خشک ۱۰ دقیقه بدون خطا و تمرین بستن موفق
- [ ] `config/runtime.yaml`: `pause_entries: false`، `size_mult: 1.0`، زمان‌ها روی `2026-10-23` (جمعه ۱ آبان)
- [ ] موجودی والکس: ~۵,۰۰۰,۰۰۰ تومان آزاد، بدون سفارش باز؛ snapshot موجودی‌های اولیه ثبت شد
- [ ] داده کندل در ۳۰ دقیقه اخیر به‌روز (`run_live.py` خودش هم fetch می‌کند)
- [ ] `git check-ignore .env` درست؛ commit با مسیرهای صریح؛ `git tag live-v1`؛ push
- [ ] تنظیمات ویندوز (Sleep، درب، Update، QuickEdit)؛ اینترنت پایدار

---

## ۹. راهنمای اجرا برای محسن (Anaconda Prompt)

```bat
cd C:\Users\rajman\Documents\wallex-bot
python tools/llm_check.py --llm box_sol61 --n 2
python tools/live_smoke.py --symbol PEPETMN --tmn 60000 --yes
python tools/live_smoke.py --executor --symbol PEPETMN --tmn 65000 --yes
python run_live.py --run-id live_20261022 --accounts C7 --llm box_sol61,box_sol6,box_sol56 --end "2026-10-23 22:50"
```
(`run_live.py` خودش `--live` و در صورت وجود وضعیت `--resume` را اضافه می‌کند و قبل از هر شروع داده را به‌روز می‌کند.)

**پایش:** `runs\live_20261022\status.json` (هر ۵ دقیقه)، `heartbeat.json`، `supervisor.log`، `child.log`.

**کنترل‌ها (ویرایش `config\runtime.yaml`؛ در چرخه بعد اعمال و لاگ می‌شود):**

| می‌خواهید | تغییر |
|---|---|
| تعویض مدل | `llm_profile: box_sol6` (یا هر پروفایل/زنجیره) |
| توقف ورود جدید، ادامه مدیریت | `pause_entries: true` |
| کوچک کردن سایزها | `size_mult: 0.5` |
| کم کردن هزینه LLM | `native_every_min: 60` |
| بستن اضطراری همه (الگوریتمی) | `flatten_now: true` |

**ری‌استارت با کد جدید (P1):** commit + tag `live-v2` ← Ctrl+C در پنجره ناظر (امن است: هیچ فروشی انجام نمی‌شود، فقط ذخیره) ← اجرای دوباره همان دستور `run_live.py` با همان `--run-id` (خودکار resume و آشتی). در این فاصله استاپ‌های سرور (اگر پشتیبانی شود) سر جایشان‌اند؛ اگر استاپ فقط کلاینتی است، ری‌استارت را کوتاه نگه دارید.

**پایان (جمعه):** ساعت ۲۲:۵۰ در اپ والکس چک کنید چیزی جز خرده‌های زیر ۵۰ هزار نمانده؛ `flatten_report.json` را ببینید؛ commit و tag `final`.

---

## ۱۰. ریسک‌ها و پاسخ‌ها

| ریسک | تشخیص | پاسخ خودکار | اقدام دستی |
|---|---|---|---|
| BoxAPI قطع یا کند | خطا/timeout در لاگ LLM | زنجیره sol61 ← sol6 ← 5.6-sol؛ همه شکست ← ورود نه، مدیریت ادامه | تعویض داغ پروفایل (حتی موقتاً CodeCraft) |
| تمام شدن بودجه دلاری | دفتر `boxapi` | در ۸۰٪: اسکن آزاد و بازتاب قطع؛ در ۱۰۰٪: ورود نه؛ پوزیشن‌ها با کد مدیریت می‌شوند | شارژ BoxAPI و بالا بردن `LLM_BUDGET_USD` |
| فیلد استاپ نادیده گرفته شود (فروش فوری بعد از هر ورود) | پروتکل پیوست D | `stop_supported: false` ← فقط استاپ کلاینت | — |
| معنای `value`/`locked` موجودی اشتباه فهمیده شود | تست دود ۵b | پرچم `balance_value_includes_locked`؛ آشتی روی موجودی کل؛ T6(۷) | — |
| exception در hook (خرید دوباره) | `hook_error` | hook بعد از pending؛ `mark_unknown` و آشتی | — |
| Ctrl+C یا کرش | heartbeat قدیمی | بدون فروش؛ ذخیره؛ ناظر ری‌استارت با resume و آشتی | اگر ناظر هم مرد: همان دستور را دوباره اجرا کنید |
| استاپ سرور پشتیبانی نشود یا پر نشود | تست دود / `fast_tick` | استاپ کلاینت در thread جدا هر ۱۵ ثانیه | — |
| واگرایی کاغذ و واقعیت | آشتی ۵ دقیقه‌ای | relink / adopt / close و لاگ | — |
| پر شدن جزئی زیر حداقل سفارش | مقدار پرشده | خرید تکمیلی `max(هدف − پرشده، ۵۲k)` | — |
| رد سفارش روی یک ارز (`isRisky` در PUMP یا بازه قیمت منصفانه) | پاسخ سفارش | حذف پوزیشن کاغذی؛ بعد از ۲ رد، ارز غیرفعال | — |
| دفتر کم‌عمق در بستن پایانی | حلقه بستن | از ۲۲:۰۰ هر دقیقه با نردبان لغزش تا ۲۲:۴۵ | ۲۲:۴۵: `flatten_now` و بررسی |
| سقوط هم‌زمان بازار | BTC ۱۵ دقیقه | ورود نه؛ تنظیم‌کننده افت؛ سقف گرما با فاصله مؤثر | — |
| ویندوز: Sleep، Update، QuickEdit، فایل باز در Excel، `zoneinfo` | supervisor/child.log | آفست ثابت ‎+03:30‎؛ retry نوشتن فایل؛ خروجی به فایل | تنظیمات P0-00 |
| rate limit والکس (۴۲۹) | کد پاسخ | backoff؛ مصرف ~۴۰–۶۰ درخواست در دقیقه زیر سقف ۶۰۰ | — |
| تصمیم ناپایدار LLM | لاگ | گاردریل‌ها و بازه‌ها مستقل از مدل | — |

---

## ۱۱. تحویلی‌ها و نگاشت به بارم داوری

| بارم | امتیاز | شاهد |
|---|---|---|
| درصد سود | ۹۰ | `trades_live.csv` (fill واقعی)، `flatten_report.json` |
| کنترل ضرر | ۳۰ | منحنی ارزش واقعی (`status.json` هر ۵ دقیقه)، لاگ گذارهای تنظیم‌کننده افت |
| فعال بودن | ۱۵ | ۱۰–۱۵ معامله، پوشش ساعتی، سطح probe |
| پایداری اجرا | ۱۵ | `cycle_errors.json`، `supervisor.log`، نرخ خطای سفارش و LLM |
| منطق استراتژی | ۴۰ | این سند §۳ و §۵ (مسلح‌سازی، شواهد پایدار در دو نیمه) |
| AI واقعی | ۳۵ | LLM تصمیم‌گیرنده (ورود، سطح سایز، SL/TP، زمان، thesis، invalidation)، اسکن آزاد، حافظه بازتابی، AI lift زنده در برابر دوقلو، lift تست B |
| مدیریت ریسک | ۳۰ | سقف ریسک ۰٫۳٪، گرما، ۵۰٪ نقد، استاپ سرور روی ۱۰۰٪ پوزیشن‌ها، تنظیم‌کننده افت |
| تکرارپذیری | ۲۵ | tagها، `code_version` در هر رکورد، `requirements.txt`، دستور یکتا، رگرسیون |
| بک‌تست و سایه | ۲۰ | تست‌های A/B/C قبلی + مطالعه این سند + دوقلوی کاغذی زنده |

---

## ۱۲. پیوست‌ها

### A1 · `config/llm.yaml` (افزودنی‌ها)

```yaml
profiles:
  box_sol61:
    backend: openai_compat
    base_url: https://ai.boxapi.ir/ai/v1
    model: gpt-6.1-sol
    api_key_env: BOXAPI_API_KEY
    reasoning_effort: low          # gpt-6.1-sol: low|medium|high|xhigh|max (no none/minimal)
    timeout_s: 40
    max_output_tokens: 700         # max_completion_tokens = 700 + 2500
    ledger: boxapi
    cache: false                   # live prompts are unique; never reuse an old answer
    price_usd_per_m: {input: 2.0, cached_input: 0.10, output: 10.0}   # public list price; verify vs BoxAPI dashboard
    # user_agent: "opencode/1.19.3"   # only if Cloudflare blocks the default
  box_sol6:
    backend: openai_compat
    base_url: https://ai.boxapi.ir/ai/v1
    model: gpt-6-sol
    api_key_env: BOXAPI_API_KEY
    reasoning_effort: low
    timeout_s: 40
    max_output_tokens: 700
    ledger: boxapi
    cache: false
    price_usd_per_m: {input: 2.0, cached_input: 0.20, output: 10.0}
  box_sol56:
    backend: openai_compat
    base_url: https://ai.boxapi.ir/ai/v1
    model: gpt-5.6-sol
    api_key_env: BOXAPI_API_KEY
    reasoning_effort: low
    timeout_s: 45
    max_output_tokens: 700
    ledger: boxapi
    cache: false
    price_usd_per_m: {input: 4.0, cached_input: 0.40, output: 20.0}

roles:
  decide:  [box_sol61, box_sol6, box_sol56]
  native:  [box_sol61, box_sol6, box_sol56]
  reflect: [box_sol61, box_sol6, box_sol56]
  risk_manager: [sol56, terra56]      # C3 only (offline)
  orchestrator: [sol56, terra56]      # C6 only (offline)
  card_eval: [luna56]

defaults:
  temperature: 0
  json_retries: 1
  max_parallel_calls: 2
  calls_per_hour: 120
  cache: true                         # global default stays true (offline tests); box profiles override with false
  ledgers:
    codecraft: {token_cap: 900000}
    boxapi:    {usd_cap: 2.7, low_priority_stop_frac: 0.8}   # env: LLM_BUDGET_USD; ledger dir: LLM_LEDGER_DIR
```

### A2 · `config/runtime.yaml`

```yaml
llm_profile: "box_sol61,box_sol6,box_sol56"   # hot-swap: any profile or comma chain (applied next cycle)
pause_entries: false          # true = no new entries, keep managing open positions
size_mult: 1.0                # 0..1.5 (governor still applies; floor 65k)
native_every_min: 30          # 15..240 (elapsed-time schedule)
max_positions: 6
probe_only_after: "2026-10-23 14:00"     # Tehran (+03:30)
no_new_entries_after: "2026-10-23 20:00"
flatten_start: "2026-10-23 22:00"
hard_deadline: "2026-10-23 22:45"
flatten_now: false            # emergency algorithmic flatten
# prompt_version: v2          # fixed in P0 (decide/native v2, reflect v1); hot-swap of prompts is P1-06
```

### A3 · `combos.py` — C7

```python
"C7": dict(name="Unified evidence-calibrated dip agent (LLM decides)", scanners=["M1"], native=True,
           min_liq=0.0, coins=LIQUID,
           size=150_000, size_band=(65_000, 220_000),
           size_tiers={"probe": 65_000, "standard": 150_000, "high": 220_000},
           risk_cap_pct=0.003, heat_cap_pct=0.015, stop_limit_offset=0.015,   # eff = sl + 1.5% + 0.6% fees
           sl_band=(4.5, 5.0), base_sl=5.0, tp_band=(5.0, 7.0), base_tps=[(6.0, 1.0)], time_stop=1440,
           be_pct=0, trail_atr=0, time_stop_min_profit=0, limit_valid=10,     # explicit: no BE / trailing
           max_pos=6, max_exposure=0.50, cluster_caps={"meme": 3}, entries_per_hour=3, sl_cooldown=(1, 240),
           daily_stop=0.99, dd_soft=0.02, dd_hard=0.04, dd_resume=0.03,
           btc15_block=-0.005, arm_window_min=360, arm_dd_pct=-8.0,
           night_hours=(0, 6), night_tier_cap="standard",
           entry="market", regime_ok=None, need_btc_bull=False, dd_cap=0.04),
```
(`entry="market"` یعنی در موتور کاغذی ورود در کندل بعد؛ در واقعیت اجراکننده با Limit قابل‌اجرا می‌خرد. با ۵M و SL ۵٪: high ≈ ۲۱۱k پس از سقف مؤثر، standard ۱۵۰k، probe ۶۵k.)

### A4 · `config/broker.yaml` (پیش‌فرض امن تا قبل از تست دود)

```yaml
stop_supported: false            # boolean only; anything else = false. Set true ONLY by tools/live_smoke.py protocol
stop_type: STOP_LIMIT
stop_field: stop_Price           # tried first (MCP schema), then stopPrice, stop_price
limit_rel_ok: false              # real-config stop (limit = stop*0.985) accepted?
balance_value_includes_locked: null   # true|false from smoke 5b; null -> total = value + locked (conservative) + warning
dup_id_rejected: null            # true -> safe to resubmit with same client_id after timeout
cancel_method: query             # query | body | path
buy_fee_in_base: null
max_entry_slip: 0.004
max_limit_distance: 0.03         # entries/exits never priced > 3% from best bid/ask (stops exempt)
exit_slip_ladder: [0.008, 0.012, 0.02, 0.03]   # single ladder for exits and flatten
poll_s: 1
fill_wait_s: 15
```

### B1 · `prompts/decide_v2.md`

```text
You are the trading decision-maker of an autonomous, spot-only crypto agent on the Wallex exchange (Iran, prices in TMN).
Goal for the rest of the contest window: maximise net TMN profit while keeping drawdown low. Activity is also scored: the
agent should make about 10-15 sensible trades per 24h. When a setup is only average, prefer a small "probe" over skipping.
Code enforces hard limits after you (bands, size caps, stops, risk caps, end-of-window); out-of-band values are clamped.

How to read the inputs (Wallex is thin by global standards):
- share_of_minutes_traded / liquidity_rank_pct: even BTC trades in only ~68% of minutes on Wallex. 0.25-0.70 is NORMAL for
  the coins you see; they are already the most liquid on the exchange. Do NOT reject for "thin trading" unless
  share < 0.15 or minutes_since_last_trade > 10.
- A dip setup is by construction below EMA50 with low RSI and often high ADX. "Downtrend" alone is not a reason to skip.
- nearby_resistance_pct is NOT the trade target. Targets are 5-7%; the edge needs 12-24h holds.
- BTC crashes (BTCUSDT 15m <= -0.5%) and first dip signals are already filtered out by code.

Evidence: 35 days of 1-minute Wallex data, 9 liquid TMN coins, pessimistic fills (0.30% taker fee, spread, slippage),
plan SL 5% / TP 6% / max hold 24h. Net result per trade (n = sample size). * = same sign in both halves of the sample.
| condition                                         |   n | net/trade | win |
| repeat dip signal (>=1 earlier signal in 6h) *     | 119 |  +0.7%    | 61% |
| 2-3 earlier signals in 6h *                       |  51 |  +1.5%    | 69% |
| price >= 8% below its 6h high *                   |  30 |  +3.5%    | 87% |
| BTC above its 1h EMA50 *                          |  47 |  +1.3%    | 66% |
| Tehran hour 17-23 *                               |  72 |  +1.0%    | 61% |
| Tehran hour 00-05                                 |  65 |  -0.5%    | 43% |
| coin -6% or worse over 24h                        |  53 |  +0.4%    | 62% |
| holding only 4h instead of 24h                    | 233 |  -0.4%    |  -  |
| stops <= 3% (noise stop-outs)                     | 233 |  -0.3%    |  -  |
These are noisy, in-sample priors, not rules. Combine them with what you see now.

Procedure:
1. setup_quality 0-100: start at 50. Add for repeat/deeper dips, >= 8% below the 6h high, BTC above its 1h EMA50, evening
   hours, capitulation volume, a stabilising last 15-30 minutes. Subtract for BTC weakness (1h/4h), night hours, no trades in
   the last minutes, a coin still making new lows on rising volume, a lesson that says avoid.
2. size_tier: quality >= 70 -> "high"; 55-69 -> "standard"; 40-54 -> "probe"; < 40 -> action "skip".
3. sl_pct inside the allowed band (default 5.0); one take-profit inside the band (default 6.0, portion 1.0);
   time_stop_min <= allowed (default: the maximum allowed).
4. thesis: why this should revert, citing the evidence you used. invalidation: what would prove it wrong.

Answer with ONE JSON object only, no prose:
{"action":"enter"|"skip","size_tier":"probe"|"standard"|"high","setup_quality":0-100,"sl_pct":number,
 "tp_plan":[{"pct":number,"portion":1.0}],"time_stop_min":number,"confidence":0..1,
 "thesis":"<=25 words","invalidation":"<=15 words"}
```

### B2 · `prompts/native_v2.md`

```text
You are an autonomous spot-only crypto trader on Wallex (TMN). Every 30 minutes you see a compact table of the 9 most
liquid coins, the market and your portfolio. Pick AT MOST 2 coins to buy now (only coins not already held), or none.
Goal: net TMN profit with low drawdown; about 10-15 sensible trades per 24h across the whole agent (see contest block).
Round-trip cost ~0.9%. Code enforces size caps, stops, BTC-crash and end-of-window rules after you.

Evidence (35 days Wallex, pessimistic fills, SL 5% / TP 6% / max 24h): buying deep dips on these coins worked best when
the coin had repeated dip signals in the last 6h (+0.7%/trade), was >= 8% below its 6h high (+3.5%), BTC was above its
1h EMA50 (+1.3%), or in Tehran evening hours (+1.0%). Chasing momentum (top 4h gainers) LOST money (-7% over 35 days in
the rule version). Night hours 00-05 were negative. Holding < 8h was negative. share_traded 0.25-0.70 is normal here.

For each buy use the same scoring as the decision rubric: setup_quality 0-100 -> size_tier (>=70 high, 55-69 standard,
40-54 probe, <40 do not buy). Prefer doing nothing over a weak idea, but remember activity is scored.

Answer with ONE JSON object only:
{"buys":[{"symbol":"<SYMBOL exactly as in the table>","size_tier":"probe"|"standard"|"high","setup_quality":0-100,
 "sl_pct":number,"tp_plan":[{"pct":number,"portion":1.0}],"time_stop_min":number,"confidence":0..1,
 "thesis":"<=25 words","invalidation":"<=15 words"}],"market_view":"<=20 words"}
```

### C · schema تصمیم (`policies.py`)

```python
DECISION_SCHEMA["properties"].update({
    "size_tier": {"enum": ["probe", "standard", "high"]},
    "setup_quality": {"type": "number", "minimum": 0, "maximum": 100},
})
NATIVE_SCHEMA = {"type": "object", "required": ["buys"], "properties": {"buys": {"type": "array", "maxItems": 2,
    "items": {"type": "object", "required": ["symbol", "size_tier", "sl_pct", "thesis"],
              "properties": {"symbol": {"type": "string"}, "size_tier": {"enum": ["probe", "standard", "high"]},
                             "setup_quality": {"type": "number", "minimum": 0, "maximum": 100},
                             "sl_pct": {"type": "number", "minimum": 0.2, "maximum": 10}}}}}}
```

### D · روال تست دود

**بخش ۱ — کلاینت خام** (`tools/live_smoke.py --symbol PEPETMN --tmn 60000 --yes`؛ بدون `--yes` اجرا نشود):
1. `markets()`: قواعد PEPETMN و BTCTMN (دقت، `minNotional`، `isMarketTypeEnable`) ذخیره.
2. `balances()`: TMN آزاد ≥ ۱۵۰ هزار؛ snapshot همه موجودی‌ها. `client_id`ها با **قالب و طول** اجرای واقعی (`c7-<epoch>-PEPETMN-buy-1`).
3. (بی‌هزینه) `MARKET BUY` کوچک روی PEPETMN ← انتظار رد؛ متن خطا ثبت.
4. `LIMIT BUY` ~۶۰ هزار با قیمت راه‌رفته روی asks ← پایش `get_order` تا FILLED؛ ثبت `executedQty`، `executedSum`؛ از `account_trades` معلوم شود کارمزد خرید از PEPE کم شده یا TMN (`buy_fee_in_base`).
4b. **تکرار client_id:** یک `LIMIT BUY` غیرقابل‌اجرا (۳٪ زیر bid، حداقل مقدار) با id جدید، و بلافاصله همان id دوباره ← رد شد؟ (`dup_id_rejected`) ← هر دو لغو.
5. **پروتکل استاپ (امن‌ترین حالت اول):** برای هر نام فیلد به ترتیب `stop_Price`، `stopPrice`، `stop_price`: `STOP_LIMIT SELL` کل موجودی با `stop = قیمت × 0.95` و `limit = قیمت × 1.03` (اگر فیلد نادیده گرفته شود، این فقط یک Limit فروش بالای بازار است و پر نمی‌شود) ← `get_order` ← **پشتیبانی فقط وقتی تأیید می‌شود که:** نوع `STOP_LIMIT` باشد، **قیمت استاپ دقیقاً برگردانده شود**، وضعیت غیر FILLED بماند، و بعد از ۱۰ ثانیه هم فعال نشده باشد. **بین هر تلاش سفارش قبلی لغو و آزاد شدن موجودی تأیید شود.** اگر صرافی به خاطر `limit > stop` رد کرد، تلاش بعدی با همان فیلد و `limit = stop × 0.985`.
5b. اگر فیلدی تأیید شد: یک استاپ با **پیکربندی واقعی** (`stop = قیمت × 0.95`، `limit = stop × 0.985`) ← اگر فوراً پر شد یعنی فیلد نادیده گرفته شده: `stop_supported: false` (هزینه ~۳۰۰ تومان)؛ اگر به خاطر بازه قیمت رد شد: `limit_rel_ok: false`. در حالت قفل ببین `value` موجودی کم می‌شود یا فقط `locked` بالا می‌رود (`balance_value_includes_locked`).
6. لغو با `client_id` (اول `query`، بعد `body`، بعد `path`) ← تأیید آزاد شدن موجودی (`cancel_method`).
7. `LIMIT SELL` کل موجودی روی bidها ← FILLED.
8. موجودی نهایی و همه پاسخ‌ها در `runs/smoke/<ts>.json`؛ نوشتن `config/broker.yaml` فقط با مقادیر بولین/عددی قطعی.

**بخش ۲ — اجراکننده** (`--executor --tmn 65000`): `enter` → `place_stop` (اگر `stop_supported`) → دو بار `fast_tick` → `exit` با یک حساب ساختگی؛ تأیید: یک خرید، یک استاپ، لغو استاپ قبل از فروش، یک فروش، `live_state.json` درست، هیچ سفارش باز.

### E · وضعیت و لاگ‌ها (`runs/<run>/`)

| فایل | محتوا |
|---|---|
| `live_state.json` | منبع ادامه بعد از ری‌استارت (بدون pickle): `start_cash`، `start_ts`، `epoch`، `seq`، `initial_balances`، `gov_state`، `disabled_coins`، شمارنده‌ها به timestamp، و برای هر ارز `{qty, avg_px, fees, entry_cid, stop_cid, stop_px, stop_limit_px, server_stop, sl_pct, tp_pct, time_stop_deadline_ts, size_tier, opened_ts, status}` |
| `decisions.jsonl` (در پوشه حساب) | هر کارت: نتیجه کد (رد/مسلح/پذیرش)، تصمیم LLM، سطح سایز، سایز، ریسک |
| `llm_C7.jsonl` | ورودی/خروجی کامل هر فراخوانی، `profile`، تأخیر، `usage`، `cost_usd`، `prompt_version`، `code_version` |
| `orders.jsonl` | همه درخواست/پاسخ‌های والکس (بدون کلید) |
| `trades_live.csv` | معاملات با fill واقعی: ارز، ورود/خروج، مقدار، کارمزد، سود، دلیل خروج، thesis، `size_tier`، `code_version` |
| `status.json` (P0) / `status.md` (P1) | وضعیت لحظه‌ای هر ۵ دقیقه |
| `control_changes.jsonl` | هر تغییر فایل کنترل |
| `flatten_report.json` | نتیجه بستن پایانی |

### F · ویدئوی ۵ دقیقه‌ای (طرح)

۱) مسئله و قیود والکس (هزینه ~۱٪، نقدشوندگی کم، Market بسته) · ۲) مسیر شواهد: ۲۳۶ استراتژی ← مطالعه لبه ← سه تست ← کشف «اولین سیگنال بد است» · ۳) معماری یک موتور سه حالت + اجراکننده آینه‌ای · ۴) نقش LLM با یک تصمیم واقعی از لاگ (ورودی، thesis، سطح سایز) · ۵) مدیریت ریسک و تنظیم‌کننده افت · ۶) نتیجه اجرای واقعی و AI lift در برابر دوقلو · ۷) اجرای مجدد با یک دستور.

### G · بازتولید شواهد

اسکریپت‌ها در `tools/evidence/` (فقط‌خواندنی، بدون سفارش؛ از ریشه مخزن اجرا شوند؛ هر کدام ~۱ تا ۳ دقیقه):

```bat
python tools\evidence\a3.py   :: لبه ناخالص M1/M4/M2/M7 و تفکیک بر اساس نقدشوندگی، رژیم، BTC، بازده ۲۴ ساعته، تتر
python tools\evidence\a4.py   :: پلن‌های خروج مختلف روی کارت‌های نقد
python tools\evidence\a6.py   :: SL ثابت در برابر ATR و سقف ریسک
python tools\evidence\a7.py   :: افق نگهداری و پرتفوی منطق فعلی
python tools\evidence\a8.py   :: اولین/تکراری، افت ۶ ساعته، BTC، ساعت روز (جدول §۳.۲)
python tools\evidence\a9.py   :: پرتفوی «مسلح کن، بعد ماشه» و پنجره‌های ۲۴٫۵ ساعته
```
(اعداد با جابه‌جا شدن بازه ۳۵ روزه کمی تغییر می‌کنند؛ اجرای ۲۲ اکتبر ساعت ۲۱: ۲۳۲ کارت، اولین سیگنال ‎−۰٫۳۰٪‎، تکراری ‎+۰٫۷۴٪‎.)

---

*پایان پروپوزال. منابع: `docs/PROJECT_REPORT.md` v1.1، `NOTES.md`، `docs/BACKTEST_PLAN.md`، کد مخزن در commit `5897f87`، خروجی‌های `runs/run_20261022_1226/`، API و MCP والکس (فقط‌خواندنی، ۲۲ اکتبر)، مستندات API والکس ([api-docs.wallex.ir](https://api-docs.wallex.ir/))، قیمت‌گذاری عمومی مدل‌ها ([benchlm.ai](https://benchlm.ai/openai/api-pricing)، [aicatchup.com](https://aicatchup.com/news/openai-gpt-6-1-sol)).*
