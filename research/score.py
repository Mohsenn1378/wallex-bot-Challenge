import json, re

rows = []
for f in 'abc':
    for r in json.load(open(f'/tmp/claude-0/strategies/{f}.json')):
        r['grp'] = f
        rows.append(r)

# ---------- 1. dedupe (index of duplicate -> index kept) ----------
DUP = {125: 52, 126: 53, 127: 54, 128: 55, 130: 56, 131: 57, 231: 4, 213: 153, 214: 152, 168: 91}
for d, k in DUP.items():
    rows[k].setdefault('extra_sources', []).append(rows[d]['source_url'])

# ---------- 2. archetype per index ----------
A = {}
def put(arch, *idx):
    for i in idx: A[i] = arch
put('MR_DIP', 0,1,2,3,4,5,6,8,9,12,13,15,22,24,25,26,27,28,30,31,32,33,39,45,90,124)
put('ENSEMBLE', 29)
put('MOMENTUM', 7,89)
put('OSC', 10,14,17,19,20,21,34,35,36,38,43,44,60,63,66,73,80,177,182)
put('CROSSOVER', 11,16,18,40,56,59,64,65,69,71,78,79,82,83,84,85,86,87,174,178,180,185,186)
put('TREND', 23,41)
put('TREND_PULLBACK', 37,52,53,57,70,72,74,75,76,88,129,132,179)
put('MR_VWAP', 54,55,58,245)
put('BREAKOUT', 42,50,61,62,67,68,77,103,112,113,116,117,243)
put('BREAKOUT_ORB', 93,122,176)
put('BREAKOUT_SESSION', 104,173)
put('VOL_BREAKOUT', 166,167)
put('PA_SMC', 94,95,96,97,98)
put('PA_SWEEP', 101,102,202)
put('PA', 106,107,114)
put('PA_CANDLE', 110,111)
put('RANGE', 108,184)
put('LEVEL', 81,109)
put('GAP', 118,119,121,193)
put('SESSION', 123)
put('RS', 133)
put('ORDERFLOW', 136,137,138,141,143,144,163)
put('CVD', 139,140,145,146)
put('EXEC', 147)
put('MM', 148,149,150,151,152)
put('MM_WIDE', 165)
put('GRID', 155,156,157,158)
put('DCA', 159)
put('LEADLAG', 164,210)
put('ARB_TRI', 208,209)
put('SEASONAL', 189,190,191,192)
put('PREMIUM', 194)
put('DEPEG', 196)
put('EVENT', 201)
put('RATIO', 187,188)
put('ML', 229,232,233,234,235)
put('RL', 227,228,230)
put('DL_LOB', 224)
put('REGIME', 237,238)
put('LLM', 239,240,241,242)
put('NEWS', 175)
put('OSC_NOSL', 183)
put('MICRO_REV', 223)
put('CLOSED', 181)

# criteria: ret, dd, act, stab, logic, ai, risk, repro, bt  (0-10)
BASE = {
 'MR_DIP':          [5,5,7,8,8,6,6,9,9],
 'ENSEMBLE':        [5,5,7,7,5,6,6,7,9],
 'MOMENTUM':        [4,5,6,8,7,6,6,9,9],
 'OSC':             [4,5,7,8,7,5,6,9,9],
 'OSC_NOSL':        [4,2,5,8,6,5,2,7,8],
 'CROSSOVER':       [2,5,8,8,7,5,6,9,9],
 'TREND':           [3,5,5,8,7,5,6,8,9],
 'TREND_PULLBACK':  [4,7,7,8,8,6,8,8,9],
 'MR_VWAP':         [4,7,7,8,8,6,8,8,9],
 'BREAKOUT':        [5,6,5,8,8,6,7,8,8],
 'BREAKOUT_ORB':    [3,6,2,8,7,5,7,8,8],
 'BREAKOUT_SESSION':[4,6,3,8,8,5,7,8,8],
 'VOL_BREAKOUT':    [6,6,3,9,9,5,7,9,9],
 'PA_SMC':          [4,6,5,7,6,7,7,4,5],
 'PA_SWEEP':        [5,7,5,8,8,7,8,7,7],
 'PA':              [4,7,5,7,6,7,7,6,6],
 'PA_CANDLE':       [3,6,6,8,7,6,7,8,8],
 'RANGE':           [4,6,6,8,8,7,7,7,7],
 'LEVEL':           [3,6,5,8,7,5,7,7,7],
 'GAP':             [1,6,1,8,6,4,6,7,6],
 'SESSION':         [3,6,3,8,7,5,7,7,7],
 'RS':              [5,6,6,7,8,7,7,8,8],
 'ORDERFLOW':       [3,6,7,3,6,6,6,4,2],
 'CVD':             [4,6,6,6,7,7,6,6,5],
 'EXEC':            [2,7,5,6,8,4,7,7,5],
 'MM':              [1,4,9,4,8,5,5,6,3],
 'MM_WIDE':         [4,4,6,5,8,6,5,6,3],
 'GRID':            [3,3,7,7,8,4,3,8,7],
 'DCA':             [3,2,6,8,8,4,2,8,7],
 'LEADLAG':         [6,7,7,6,9,6,7,7,6],
 'ARB_TRI':         [3,9,3,5,9,3,9,7,4],
 'SEASONAL':        [4,6,2,9,8,4,7,9,8],
 'PREMIUM':         [4,6,3,6,8,6,7,6,5],
 'DEPEG':           [1,8,0,8,8,4,8,7,3],
 'EVENT':           [3,5,1,5,6,8,5,3,3],
 'RATIO':           [3,7,3,8,8,5,8,8,8],
 'ML':              [6,6,6,7,6,10,7,7,9],
 'RL':              [4,5,7,6,4,10,5,4,6],
 'DL_LOB':          [3,5,8,3,4,10,5,3,2],
 'REGIME':          [6,7,6,7,7,9,8,7,8],
 'LLM':             [4,6,3,6,6,10,6,4,3],
 'NEWS':            [3,6,1,7,7,7,7,6,5],
 'MICRO_REV':       [2,6,8,7,7,6,6,8,8],
 'CLOSED':          [3,5,6,7,1,4,5,1,3],
}
W = [90,30,15,15,40,35,30,25,20]   # Wallex rubric points (total 300)
CRIT = ['ret','dd','act','stab','logic','ai','risk','repro','bt']

# ---------- 3. filters ----------
NOT_ON_WALLEX = {   # asset-specific and the asset is not tradable on Wallex
 46:'EURUSD/GBPUSD/AUDUSD (فارکس)',47:'EURUSD/GBPUSD/AUDUSD (فارکس)',48:'EURUSD/GBPUSD/AUDUSD (فارکس)',
 49:'EURUSD (فارکس)',51:'EURUSD (فارکس)',91:'ES فیوچرز شاخص',92:'سهام آمریکا',99:'فیوچرز YM/ES/NQ',
 100:'شاخص NAS100',105:'GBPUSD (فارکس)',115:'سهام کوچک آمریکا',120:'ES/MES فیوچرز',134:'فارکس',135:'فارکس',
 142:'Nifty فیوچرز',160:'CRV فیوچرز (CRV در والکس غیرفعال)',161:'ETHUSDT فیوچرز',162:'پرپچوال SOL/ONDO/XRP',
 169:'NQ فیوچرز',170:'DAX فیوچرز',171:'GBPJPY (فارکس)',172:'EURUSD (فارکس)',
 195:'صرافی های کره ای (KRW)',199:'میم کوین های Pump.fun سولانا',200:'کوین های گروه پامپ تلگرام',
 211:'بازارهای Polymarket',220:'LSK/ICON/XMR (در والکس نیست یا غیرفعال)',221:'سهام VIA/VIAB',236:'SPY',244:'AUDUSD (فارکس)'}
NEEDS_SHORT = {154,198,203,204,205,206,207,215,216,217,218,219,222,225,226}
NOT_EXECUTABLE = {153:'نیاز به صرافی دوم و انتقال دارایی (در حساب اعتباری غیرفعال)',
                  212:'نیاز به صرافی دوم و انتقال دارایی (در حساب اعتباری غیرفعال)',
                  197:'نیاز به وام گرفتن در DeFi (Aave/Curve)',
                  181:'منطق رمزنگاری شده؛ قابل توضیح و تحویل سورس نیست (خلاف قوانین چالش)'}
MAPPED = {104:'XAUUSD → XAUTTMN / XAUTUSDT',174:'XAUUSD → XAUTTMN / XAUTUSDT',
          175:'CL → USOONTMN (توکن نفت)',176:'CL → USOONTMN (توکن نفت)',
          167:'KRW-BTC → BTCTMN',208:'مثلث BTC/LTC/USD → مثلث BTC/USDT/TMN در والکس',
          209:'مثلث های بایننس → مثلث های TMN/USDT والکس',182:'ETH/BTC مصنوعی از ETHUSDT و BTCUSDT',
          187:'ETH/BTC مصنوعی از ETHUSDT و BTCUSDT',196:'USDCTMN',242:'فقط بخش BTC'}

NOTES = {
 'MR_DIP':'خرید دیپ زیر باند/اسیلاتور. روی جفت های پرنوسان و نقد (PEPE, DOGE, SOL, XRP, PUMP) جواب بهتری میده. حد سود باید حداقل ۲ برابر کارمزد رفت وبرگشت باشه؛ استاپ های ۵٪+ باید تنگ تر بشن.',
 'ENSEMBLE':'ترکیب چند سیگنال دیپ؛ قوی ولی پیچیده و توضیحش برای داور سخت تره.',
 'MOMENTUM':'ورود در جهت مومنتوم؛ در بازار رنج زیاد ضرر می ده.',
 'OSC':'اسیلاتور تنها؛ بهتره به عنوان فیلتر یا لایه ورود در کنار رژیم بازار استفاده بشه.',
 'OSC_NOSL':'بدون حد ضرر؛ با معیار «کنترل ضرر» داوری در تضاده.',
 'CROSSOVER':'کراس اوور/فلیپ روی ۱ تا ۵ دقیقه معاملات زیاد با حرکت کوچک می ده؛ با کارمزد ۰.۷٪ رفت وبرگشت تومانی معمولا منفی میشه.',
 'TREND':'روندی کند؛ در ۲ روز تعداد معامله کم.',
 'TREND_PULLBACK':'ورود در پولبک روند با استاپ ساختاری؛ ریسک کنترل شده ولی اهداف ۱ تا ۵ دقیقه ای باید از کارمزد بزرگ تر باشن (تایم ۵ تا ۱۵ دقیقه بهتره).',
 'MR_VWAP':'بازگشت به VWAP؛ فاصله باند تا VWAP در کریپتو باید از کارمزد بیشتر باشه.',
 'BREAKOUT':'شکست فشردگی/کانال؛ حرکت های بزرگ تر از کارمزد میده. با فیلتر حجم و رژیم خوب کار می کنه.',
 'BREAKOUT_ORB':'برای بازار سهام با زمان باز شدن طراحی شده؛ کریپتو ۲۴ساعته است و باید به شروع سشن (مثلا ۰۰:۰۰ UTC یا شروع معاملات ایران) تطبیق داده بشه.',
 'BREAKOUT_SESSION':'شکست باکس سشن آسیا/لندن؛ در کریپتو روزی یک سیگنال.',
 'VOL_BREAKOUT':'شکست نوسان لری ویلیامز؛ منطق ساده و شفاف، اهداف بزرگ تر از کارمزد. روزی یک تا دو سیگنال در هر جفت؛ با چند جفت فعالیت کافی میشه.',
 'PA_SMC':'مفاهیم ICT/SMC تفسیری اند و کدنویسی دقیقشون سخته؛ تکرارپذیری پایین.',
 'PA_SWEEP':'شکار نقدینگی/استاپ هانت قابل کدنویسی دقیق؛ استاپ پشت ویک، ریسک مشخص.',
 'PA':'پرایس اکشن نیمه تفسیری؛ کدنویسی نیاز به تعریف دقیق سطح دارد.',
 'PA_CANDLE':'الگوی کندلی تنها؛ لبه ضعیف، بهتره فقط تاییدیه باشه.',
 'RANGE':'معامله در رنج؛ AI می تونه رنج بودن بازار رو تشخیص بده.',
 'LEVEL':'سطوح پیوت/رند؛ لبه ضعیف ولی ساده.',
 'GAP':'کریپتو ۲۴ساعته گپ ندارد؛ تقریبا قابل استفاده نیست.',
 'SESSION':'وابسته به ساعت بازار سهام؛ در کریپتو ضعیف.',
 'RS':'قدرت نسبی آلت در برابر BTC؛ برای کریپتو خیلی مناسب و قابل توضیح.',
 'ORDERFLOW':'نیاز به داده تیک/دفتر سفارش؛ با REST API والکس و نبود دیتای تاریخی L2، بک تست و اجرای پایدار سخته.',
 'CVD':'از داده معاملات (trades) قابل ساختنه؛ بک تست محدود به داده ای که ضبط کنیم.',
 'EXEC':'تکنیک اجراست نه استراتژی؛ در سطح ۱ کارمزد میکر و تیکر یکسانه، صرفه فقط از اسپرده.',
 'MM':'بازارسازی روی جفت های نقد والکس: اسپرد (۰.۰۱–۰.۰۶٪) خیلی کمتر از کارمزد میکر ۰.۳۵٪/۰.۲٪ است؛ در سطح ۱ سودده نیست.',
 'MM_WIDE':'بازارسازی روی جفت های مجاز با اسپرد بیش از ۱٪ (مثل HBARTMN, IMXTMN, ETCTMN)؛ سود از اسپرد ولی ریسک موجودی و حجم کم.',
 'GRID':'گرید اسپات: فاصله هر پله باید بیشتر از ۰.۷٪ باشه؛ بدون حد ضرر و در پایان باید همه به تومان فروخته بشه.',
 'DCA':'میانگین کم کردن = افزایش ریسک؛ با معیار مدیریت سرمایه داوری در تضاده.',
 'LEADLAG':'قیمت بایننس/بازار جهانی جلوتر از والکس حرکت می کنه؛ فقط روی والکس معامله میشه. نیاز به دسترسی داده جهانی (بایننس/OKX) از سرور.',
 'ARB_TRI':'مثلث داخلی والکس (مثلا BTCTMN ↔ BTCUSDT × USDTTMN): ۳ پایه کارمزد ≈ ۰.۹٪؛ فرصت کم ولی ریسک بسیار پایین.',
 'SEASONAL':'الگوی ساعتی BTC؛ روزی یک معامله، در ۲ روز نمونه کم.',
 'PREMIUM':'سیگنال پرمیوم بین بازارها؛ معادل والکسی: پرمیوم تتر/تومان نسبت به قیمت جهانی.',
 'DEPEG':'وابسته به رخداد دیپگ؛ در ۲ روز به احتمال زیاد اتفاق نمی افته.',
 'EVENT':'وابسته به رخداد (توییت)؛ نامنظم.',
 'RATIO':'چرخش ETH/BTC با دو پایه کارمزد؛ کند.',
 'ML':'مدل یادگیری ماشین با برچسب آگاه از کارمزد؛ نقش واقعی AI در تصمیم (امتیاز ۳۵). نیاز به داده و اعتبارسنجی walk-forward.',
 'RL':'یادگیری تقویتی؛ آموزش در ۲ روز پرریسک و توضیحش سخت.',
 'DL_LOB':'یادگیری عمیق روی دفتر سفارش؛ نیاز به داده L2 تاریخی که نداریم.',
 'REGIME':'تشخیص رژیم بازار (روند/رنج/پرنوسان) و انتخاب استراتژی؛ بهترین جای استفاده واقعی از AI.',
 'LLM':'LLM برای خبر/سنتیمنت؛ استفاده واقعی AI ولی کند، غیرقطعی و بک تستش سخته. بهتره به عنوان فیلتر ریسک استفاده بشه.',
 'NEWS':'وابسته به گزارش هفتگی نفت؛ در ۲ روز حداکثر یک رخداد.',
 'MICRO_REV':'منبع خودش میگه لبه کمتر از هزینه است.',
 'CLOSED':'کد بسته.',
}

def clamp(x): return max(0, min(10, x))

out = []
for i, r in enumerate(rows):
    if i in DUP: continue
    arch = A.get(i)
    if not arch:
        assert i in NOT_ON_WALLEX or i in NEEDS_SHORT or i in NOT_EXECUTABLE or r.get('needs_short_or_futures'), (i, r['name'])
        arch = 'OSC'
    s = list(BASE[arch])
    tp, sl = r.get('typical_tp_pct'), r.get('typical_sl_pct')
    tf = (r.get('timeframe') or '').lower()
    cs = (r.get('claimed_stats') or '').lower()
    mods = []
    if tp is not None and tp < 0.7:
        s[0] -= 3; mods.append('حد سود کمتر از کارمزد')
    elif tp is not None and tp <= 1.0:
        s[0] -= 1; mods.append('حد سود ~۱٪ (بیشترش کارمزد میشه)')
    elif tp is not None and 2 <= tp <= 6:
        s[0] += 1
    if sl is not None and sl > 15:
        s[1] -= 3; s[6] -= 3; mods.append('حد ضرر خیلی باز')
    elif sl is not None and sl >= 5:
        s[1] -= 1; s[6] -= 1
    elif sl is not None and sl <= 2.5:
        s[1] += 1; s[6] += 1
    if re.search(r'\b1d\b|daily|swing|weekly', tf) and i not in (43,):
        s[2] -= 4; s[0] -= 2; mods.append('تایم فریم روزانه/سوئینگ')
    if re.search(r'tick|100ms|millisecond|sub-second|\bseconds?\b', tf):
        s[3] -= 2; s[8] -= 2; mods.append('نیاز به داده تیکی')
    if re.match(r'^(1h|30m|hours|1h,|intraday hours)', tf) or tf.startswith('1h'):
        s[2] -= 2; mods.append('تایم فریم ساعتی (معامله کم در ۲ روز)')
    if tf.startswith('1m') or tf == '1m':
        s[3] -= 1; s[0] -= 1
    neg = any(k in cs for k in ['losing','pf 0.','underperform','below costs','unprofitable','-8.15','sharpe -'])
    weak = any(k in cs for k in ['target', 'not significant', 'no win rate', 'unverified', 'claims', 'no figures', 'qualitative', 'no numbers'])
    pos = re.search(r'sharpe [1-9]|pf 1\.[3-9]|pf 2|win rate|win \d|annual', cs) and not neg and not weak
    if neg: s[0] -= 3; mods.append('شواهد منفی در منبع')
    elif pos: s[0] += 1; s[8] += 1
    s = [clamp(x) for x in s]
    score = round(sum(w*x for w, x in zip(W, s)) / 300 * 10, 1)
    status, reason = 'keep', ''
    if i in NOT_ON_WALLEX: status, reason = 'removed_asset', 'دارایی خاص خارج از والکس: ' + NOT_ON_WALLEX[i]
    elif i in NEEDS_SHORT or (r.get('needs_short_or_futures') and i not in NOT_ON_WALLEX): status, reason = 'removed_short', 'نیاز به شورت/فیوچرز/مارجین (حساب فقط اسپات)'
    elif i in NOT_EXECUTABLE: status, reason = 'removed_rules', NOT_EXECUTABLE[i]
    srcs = [r['source_url']] + r.get('extra_sources', [])
    out.append(dict(name=r['name'], family=r['family'], arch=arch, entry=r['entry'], exit=r['exit'],
        tf=r['timeframe'], tp=tp, sl=sl, stats=r.get('claimed_stats'), asset=r.get('asset_specific'),
        mapped=MAPPED.get(i, ''), sources=srcs, status=status, reason=reason,
        s=dict(zip(CRIT, s)), score=score, note=NOTES[arch], mods=mods, proposal=False))

# ---------- 4. team proposals adapted to Wallex ----------
PROPS = [
 dict(name='W1 · Global→Wallex lead-lag on TMN/USDT pairs', family='Lead-lag (adapted)', arch='LEADLAG',
  entry='Global mid (Binance/OKX) jumps > fee+spread+buffer within N sec while Wallex price lags; buy the lagging Wallex pair',
  exit='Exit on convergence to global price × USDT/TMN, or 60–120s time stop; hard SL 0.6%', tf='seconds–5m',
  sources=[rows[164]['source_url'], rows[210]['source_url']], adj=dict(ret=+1, bt=0)),
 dict(name='W2 · Agentic regime router (LLM/ML picks the playbook)', family='Regime switching (adapted)', arch='REGIME',
  entry='ML/HMM classifier labels regime every 15m (trend / range / high-vol); agent enables Vol-Breakout in trend, BB-dip in range, flat in chaos',
  exit='Exits from the active sub-strategy; agent cuts size after drawdown; daily loss limit', tf='5m–15m',
  sources=[rows[237]['source_url'], rows[238]['source_url'], rows[240]['source_url']], adj=dict(ret=+1, ai=0, logic=+1)),
 dict(name='W3 · Intra-Wallex triangular arbitrage (TMN–USDT–coin)', family='Triangular arbitrage (adapted)', arch='ARB_TRI',
  entry='When coinTMN / (coinUSDT × USDTTMN) deviates > 0.9% + depth slippage, execute the 3 legs', exit='Cycle completes back to TMN', tf='real-time',
  sources=[rows[208]['source_url'], rows[209]['source_url']], adj=dict()),
 dict(name='W4 · Wide-spread maker on allowed mid-cap pairs', family='Liquidity provision (adapted)', arch='MM_WIDE',
  entry='Quote inside the spread only on allowed pairs whose spread > 1.2% (fees 0.7%), inventory cap 10% of capital per pair',
  exit='Opposite-side fill or inventory timeout → flatten at market; kill switch on −1.5%', tf='minutes',
  sources=[rows[165]['source_url'], rows[152]['source_url']], adj=dict()),
]
for p in PROPS:
    s = dict(zip(CRIT, BASE[p['arch']]))
    for k, v in p['adj'].items(): s[k] = clamp(s[k] + v)
    score = round(sum(w*s[c] for w, c in zip(W, CRIT)) / 300 * 10, 1)
    out.append(dict(name=p['name'], family=p['family'], arch=p['arch'], entry=p['entry'], exit=p['exit'], tf=p['tf'],
        tp=None, sl=None, stats=None, asset=None, mapped='', sources=p['sources'], status='keep', reason='',
        s=s, score=score, note=NOTES[p['arch']], mods=[], proposal=True))

keep = sorted([o for o in out if o['status'] == 'keep'], key=lambda o: (-o['score'], -o['s']['ret'], -o['s']['dd']))
for rank, o in enumerate(keep, 1):
    o['rank'] = rank
    o['tier'] = 'A' if o['score'] >= 66 else ('B' if o['score'] >= 58 else 'C')
for o in out:
    o.setdefault('rank', None); o.setdefault('tier', '-')

json.dump(out, open('/tmp/claude-0/strategies/scored.json', 'w'), ensure_ascii=False)
from collections import Counter
print('total unique', len(out), Counter(o['status'] for o in out))
print('internet strategies', sum(not o['proposal'] for o in out))
print(Counter(o['tier'] for o in keep))
for o in keep[:40]:
    print(o['rank'], o['score'], o['tier'], o['arch'], '|', o['name'], o['s'])
print('...')
for o in keep[-8:]:
    print(o['rank'], o['score'], o['arch'], '|', o['name'])
