# Handoff — کارهای باز

> این فایل سفارش کار است، نه گزارش وضعیت. وضعیت روز (انبار، نصب، اتصال ایجنت‌ها، ورودی‌ها
> و بررسی سریع) در [`docs/operations.md`](docs/operations.md) است؛ آن سند را به‌جای این یکی
> برای راه‌اندازی بخوانید. بعد از تمام‌شدن کارها این فایل را حذف کنید یا از نو بنویسید.

منبع این فهرست: بازبینی کد ۰.۲.۰ (دو بازبینی مستقل). هر مورد با بازتولید واقعی تأیید شده
مگر جایی که «اندازه‌گیری لازم است» نوشته شده باشد.

## نقطهٔ شروع

- نسخه ۰.۲.۰؛ HEAD همان `dcb5d4d` است (تغییرات مستندات این دور هنوز commit نشده‌اند).
- تست پایه: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` → ۹۶ تست سبز.
  pytest نصب نیست و لازم هم نیست؛ تست‌های تازه را در همان فایل‌های موجود و با `unittest` بنویسید.
- تصمیم‌های بسته در `docs/design.md` §۱۱ هستند؛ بدون دلیل تازه بازشان نکنید.
- روش کار: اول بازتولید، بعد اصلاح، بعد همان بازتولید و همان تست‌ها دوباره. اگر تغییری رفتار
  عمومی را عوض کرد، README و `docs/` را در همان کار به‌روز کنید.
- commit و push فقط با درخواست کاربر.
- تست‌ها `MEM_ROOT=$(mktemp -d)` می‌خواهند؛ هرگز روی انبار واقعی اجرا نشوند.

## تصمیم‌هایی که باید قبل از کد گرفته شود

| # | پرسش | پیشنهاد |
|---|---|---|
| D1 | `mem read` روی `MEMORY.md` باید کار کند یا رد شود؟ | رد شود؛ با `write`/`forget` هم‌رفتار و `mem index` جای آن است. |
| D2 | کلیدهای فرانت‌مترِ دست‌نویس (مثل `title`) در بازنویسی بمانند؟ | بمانند، فقط اسکالرها: `render` می‌تواند بنویسدشان و امروز هر بازنویسی دورشان می‌ریزد. توجه: `Memory.title` از `name` ساخته می‌شود، نه از کلید `title`. |
| D3 | آستانهٔ ادغام (۰.۸) عوض شود، شرط تازه بگیرد، یا فقط مستند شود؟ | اول روی دادهٔ واقعی بشمارید (با اجازهٔ کاربر)، بعد با کاربر تصمیم بگیرید. |
| D4 | `description` چندخطی: رد شود یا پشتیبانی شود؟ | در نوشتن رد شود؛ پارس `>`/`\|` فقط اگر ارزان بود. |
| D5 | `originSessionId` در نوشته‌های MCP: آرگومان `session` بگیرد یا فیلد حذف شود؟ | تصمیم با کاربر؛ تا آن‌وقت فیلد خالی دروغ است. |
| D6 | بایت غیر UTF-8 در `mem read`/MCP: جایگزینی یا خطا؟ | `errors="replace"` مثل `store.load`، تا یک فایل دست‌نویس کل ابزار را نشکند. |

## A. `mem read` مسیر دلخواه و فایل غیر UTF-8 را می‌خواند — P1

**بازتولید** (دایرکتوری scope باید وجود داشته باشد، پس اول یک یادداشت بنویسید):

```bash
export MEM_ROOT=$(mktemp -d) PROJ=$(mktemp -d)
echo SECRET-DATA > "$MEM_ROOT/secret.txt"
./bin/mem write --scope project --cwd "$PROJ" --path n.md --name n \
  --description d --if-version new --content body
./bin/mem read --scope project --cwd "$PROJ" --path ../../secret.txt
# خروجی ۰ و {"path": ".../projects/<slug>/../../secret.txt", ..., "content": "SECRET-DATA"}
```

مسیر `..` پاک‌سازی نمی‌شود و `load` هر فایلی را که وجود داشته باشد می‌خواند؛ `--path` مطلق هم
همین‌طور کار می‌کند. `MEMORY.md` هم الان خوانده می‌شود (با نام `MEMORY` و نوع `project`) —
یعنی D1 تصمیم دربارهٔ تغییر رفتار است، نه رفع باگ.

**علت:** `cmd_read` در [`memlib/cli.py:71`](memlib/cli.py#L71) مسیر را مستقیم به دایرکتوری
scope می‌چسباند و `store._check_filename` را صدا نمی‌زند؛ `write`، `delete` و MCP همه این
بررسی را دارند، پس فقط CLI نشت دارد.

**اصلاح:** در `cmd_read` اول `store._check_filename(args.path)` و بعد `store.load(...)`. اگر
تابع را عمومی‌تر کردید (`check_filename`)، `write`/`delete` را هم به آن منتقل کنید تا یک نسخه
بماند. طبق D1 رفتار `MEMORY.md` را مشخص کنید.

**نیمهٔ دوم — رمزگشایی سخت‌گیرانه:** [`memlib/cli.py:84`](memlib/cli.py#L84) و
[`memlib/cli.py:223`](memlib/cli.py#L223) با `encoding="utf-8"` بدون `errors` می‌خوانند؛
برای یادداشت دست‌نویس غیر UTF-8 تراک‌بک می‌دهند. `store.load` خودش `errors="replace"` دارد
([`memlib/store.py:210`](memlib/store.py#L210))، پس همین سیاست را در CLI هم به کار ببرید.
در MCP، `_current_payload` ([`memlib/mcp.py:258`](memlib/mcp.py#L258)) فقط `OSError` را
می‌گیرد؛ `UnicodeDecodeError` از آن رد می‌شود و به `internal` می‌رسد. طبق D6.

**تست‌ها:** `read --path ../../secret.txt` (خارج از scope)، `--path` مطلق، `--path MEMORY.md`
(طبق D1)، و فایل با بایت `0xff` در بدنه — همه بدون تراک‌بک و با JSON خطا (یا محتوای درست، طبق D6).

**معیار انجام:** هیچ ورودی `--path` نتواند بیرون از دایرکتوری scope بخواند؛ هیچ بایتی تراک‌بک
نسازد؛ MCP تغییری در رفتار ندهد.

## B. بازنویسی، متادیتای یادداشت را بازنشانی می‌کند — P1

**بازتولید:**

```bash
export MEM_ROOT=$(mktemp -d) PROJ=$(mktemp -d)
./bin/mem write --scope project --cwd "$PROJ" --path u.md --name prefers-vim \
  --description "prefers vim" --type user --if-version new --content "x"
V=$(./bin/mem read --scope project --cwd "$PROJ" --path u.md | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
./bin/mem write --scope project --cwd "$PROJ" --path u.md --content "x, fixed typo" --if-version "$V"
./bin/mem read --scope project --cwd "$PROJ" --path u.md
```

خروجی: `name` شده `u`، `description` خالی، `type` شده `project`؛ خط ایندکس از بخش `user`
به `project` رفته و description از آن حذف شده (`- [U](u.md)`). یعنی یک «اصلاح غلط تایپی»
یادداشت را از نوع `user` به `project` می‌برد.

**علت سه‌لایه است:**

1. [`memlib/cli.py:46`](memlib/cli.py#L46) همیشه `description=args.description or ""` و
   `memory_type=args.type` می‌فرستد و پارسر برای هر دو پیش‌فرض دارد
   ([`memlib/cli.py:171-172`](memlib/cli.py#L171-L172)) — پس «حذف‌شده» از «خالی» قابل تشخیص نیست.
2. [`memlib/store.py:326`](memlib/store.py#L326) `meta` را از صفر می‌سازد:
   `name or path.stem`، `description` داده‌شده، `memory_type` داده‌شده. هر کلید دیگر
   فرانت‌متر (مثل `title`) هم دور ریخته می‌شود، هرچند `render` آن‌ها را می‌نویسد.
3. [`memlib/mcp.py:397-398`](memlib/mcp.py#L397-L398) همان کار را با `or ""` /
   `or store.DEFAULT_TYPE` می‌کند.

**اصلاح:** زنجیره را `None`-پذیر کنید: `description: str | None = None` و
`memory_type: str | None = None` در `store.write`؛ بعد از `existing = load(path)` مقدارها را
از `existing` بردارید (`name` هم همین‌طور) و فقط بعد اعتبارسنجی `type` را روی مقدار نهایی
انجام دهید. بررسی راز فقط روی `description` ورودی، نه مقدار بازیابی‌شده. در CLI پیش‌فرض
`--description`/`--type` را `None` کنید و در MCP `arguments.get(...)` بدهید (اعتبارسنجی
`type` فقط وقتی صریح آمده). اگر فایل تازه است، `None` یعنی همان پیش‌فرض امروز (`""` و
`project`). اگر D2 را قبول کردید، `meta` را با کلیدهای اسکالر `existing.meta`
(به‌جز `name`/`description`/`metadata`) شروع کنید.

**تست‌ها:** بازنویسی با فقط `content` و `if_version` (هم CLI، هم MCP) همه‌چیز را نگه دارد؛
تغییر صریح `--type`/`--description`/`--name` کار کند؛ `--force` با فقط `content` هم بازنشانی
نکند؛ `if_version=new` روی فایل موجود همچنان `conflict` بدهد؛ کلید `title` دست‌نویس بعد از
بازنویسی در فایل بماند (در `Memory.meta` دیده شود).

**معیار انجام:** هیچ فیلدی که فراخوان نفرستاده تغییر نکند؛ تست round-trip برای
`write` ← `read` با و بدون `name`/`description`/`type`.

## C. MCP آرگومان ناشناس را بی‌صدا دور می‌ریزد — P1

**بازتولید:** درخواست `memory_write` با `"cwdd"` به‌جای `"cwd"`. سرور `ok` برمی‌گرداند و
یادداشت در scope پروژهٔ سرور می‌نشیند، نه پروژهٔ مقصود. اشتباه تایپی در نام هر آرگومان
(مثلاً `pth` به‌جای `path`) هم همین‌طور بی‌صدا رد می‌شود.

**علت:** [`memlib/mcp.py:627-629`](memlib/mcp.py#L627-L629): `unknown` محاسبه می‌شود و فقط
`_log` می‌شود. اسکیمای ابزارها `additionalProperties: false` دارند، پس کلاینت درست این را
نمی‌فرستد؛ سرور نباید بپذیرد.

**اصلاح:** جای `_log`، `raise InvalidParams(f"invalid params: unknown argument(s) for {name}: …")`.
`_TOOL_PARAMS` همین حالا منبع درست است؛ یک تست تطابق آن با `properties` اسکیما اضافه کنید تا
دو نسخه از هم جدا نشوند.

**تست‌ها:** آرگومان ناشناس → کد `-32602`، هیچ فایلی نوشته نشود؛ آرگومان‌های مجاز کار کنند؛
`cwd` همچنان بر `MEMORY_PROJECT_CWD` اولویت داشته باشد.

## D. ادغام ۰.۸ می‌تواند یادداشت را بی‌صدا حذف کند — P1 (تصمیم لازم)

**اندازه‌گیری‌شده:** عددها Jaccard توکن‌های `store.tokens` هستند — همان چیزی که `_similar`
با آستانهٔ ۰.۸ می‌سنجد.

| description اول | description دوم | Jaccard | ادغام؟ |
|---|---|---|---|
| `pnpm, not npm` | `use pnpm not npm` | ۰.۷۵ | نه |
| `deploy to production with make release and tag` | `deploy to staging with make release and tag` | ۰.۷۵ | نه |
| `deploy with make release and tag` | `deploy with make release and tag blue` | ۰.۸۶ | **بله** |
| `run unit tests before every commit` | `run unit tests before every commit only` | ۰.۸۶ | **بله** |

پس کامنت [`memlib/dream.py:43-44`](memlib/dream.py#L43-L44) («pnpm … و use pnpm … match»)
غلط است — آن جفت اصلاً ادغام نمی‌شود. در عوض جفت‌های بالا با هم‌نوعی یکسان ادغام می‌شوند:
بدنهٔ خطوط یکتا به یادداشت تازه‌تر می‌چسبد و فایل قدیمی با description و نامش حذف می‌شود،
بدون هیچ هشدار یا گزارشی. یک تغییر کوچک در جمله می‌تواند یک یادداشت مستقل را ببلعد.

**بازتولید:**

```bash
export MEM_ROOT=$(mktemp -d) PROJ=$(mktemp -d)
./bin/mem write --scope project --cwd "$PROJ" --path a.md --name release \
  --description "deploy with make release and tag" --if-version new --content "run make release; tag vX"
./bin/mem write --scope project --cwd "$PROJ" --path b.md --name release-blue \
  --description "deploy with make release and tag blue" --if-version new --content "then tag blue too"
ls "$MEM_ROOT"/projects/*/     # فقط MEMORY.md و b.md؛ a.md همان لحظه حذف شد
```

برای بازتولید حتی `mem dream` هم لازم نیست: تثبیت بعد از همان نوشتن دوم اجرا می‌شود.

**کار:**

1. با اجازهٔ کاربر، روی انبار واقعی بشمارید چند ادغام واقعی رخ داده (پیش از هر تغییر — این
   عدد تصمیم را می‌سازد و بعد از پاک‌شدن قابل بازسازی نیست).
2. گزینه‌ها را با کاربر ببرید: آستانه بالاتر؛ شرط «نام یکسان یا نزدیک» به‌علاوهٔ شباهت؛
   `mem dream --dry-run` که فقط گزارش کند؛ یا نگه‌داشتن رفتار و مستندسازی صریح.
3. مستقل از تصمیم: کامنت غلط را درست کنید و برای دو جفت بالا تست رگرسیون بگذارید (با
   `MEM_ROOT` موقت) تا رفتار فعلی قفل شود.

**معیار انجام:** رفتار ادغام یا مستند شده یا اصلاح شده؛ هیچ تستی به انبار واقعی دست نزند.

## E. `description` چندخطی فایل را خراب می‌کند — P2

**بازتولید:** `store.write(d, "n.md", "body", description="multi\nline", if_version="new")`
(یا CLI با `--description $'multi\nline'`). فایل می‌شود `description: "multi` و خط بعدی
(`line"`) داخل فرانت‌متر می‌ماند؛ `load` برمی‌گرداند `'"multi'` و ایندکس `— "multi`.
همین برای `name` هم تکرار شد: `name="a\nb"` نوشته می‌شود و `load` می‌دهد `'"a'`.

**علت:** `_quote` ([`memlib/store.py:112`](memlib/store.py#L112)) فقط `\` و `"` را escape
می‌کند و `parse` خط‌محور است ([`memlib/store.py:117`](memlib/store.py#L117)). همین برای
`name` هم برقرار است.

**اصلاح طبق D4:** در `store.write` بعد از تعیین مقدار نهایی، `\n`/`\r` در `description` و
`name` را با `StoreError` رد کنید (CLI → `invalid`, MCP → `invalid`)؛ فایل نباید اصلاً
نوشته شود. اگر پشتیبانی block scalar را انتخاب کردید، `>` و `|` را در `parse` بفهمید و
`render` را متناظر کنید.

**تست‌ها:** description با `\n` → خطا و فایل دست‌نخورده؛ `name` با `\n` → خطا؛ فایل
دست‌نویس با `description: >` (اگر پشتیبانی شد) درست خوانده شود؛ برای همهٔ رشته‌های
بدون‌خط‌جدید `write` ← `load` دقیقاً همان مقدار را بدهد.

## F. مستندات ناهم‌خوان — P2 (بعد از کد، در همان کار)

هر مورد یک ویرایش کوچک است؛ شماره‌ها برای HEAD `dcb5d4d` است.

| # | جا | مشکل | کار |
|---|---|---|---|
| F1 | [`README.md:98-99`](README.md#L98-L99) | مرحلهٔ نصب `autoMemoryEnabled: false` در `~/.claude/settings.json` را ندارد، در حالی که design §۹/§۱۰ و research آن را لازم می‌دانند | مرحله را اضافه کنید و به design §۱۰ لینک بدهید |
| F2 | [`README.md:4-5`](README.md#L4-L5) و [`README.md:131`](README.md#L131) | ادعای «همان شکل Claude Code و Qoder» و «همان اسلاگ» بدون منبع؛ وضعیت Qoder در این ماشین تأییدنشده است | به research §۱/§۲ ارجاع بدهید یا «Qoder» را بردارید؛ اسلاگ را ادعای سازگاری نکنید مگر با شاهد |
| F3 | [`docs/research.md`](docs/research.md) §۴، جدول «در طراحی ما» | طرح زمانِ نوشتن را به‌عنوان وضعیت فعلی نشان می‌دهد: سقف ۲۰۰ خط/۲۵KB، `decision`/`gotcha`، scope سشن، دروازهٔ استخراج، BM25، job شبانه، SQLite | یک خط بالای جدول: «این ستون طرحِ زمانِ تحقیق است؛ آنچه ساخته شد در design.md» — یا ردیف‌ها را با وضعیت فعلی به‌روز کنید |
| F4 | [`docs/design.md:66`](docs/design.md#L66) | جعبهٔ MCP ابزار `dream` را نشان می‌دهد؛ MCP سه ابزار دارد و تثبیت خودکار بعد از نوشتن اجرا می‌شود | سه ابزار را بنویسید و زیرش یک خط دربارهٔ تثبیت خودکار |
| F5 | [`docs/design.md:118`](docs/design.md#L118) | می‌گوید `memory_index` «متن `MEMORY.md`» را برمی‌گرداند؛ در واقع از روی فایل‌ها render می‌کند و فایل دست‌کاری‌شده را نمی‌خواند | «فهرست را از روی فایل‌ها می‌سازد و برمی‌گرداند» |
| F6 | [`README.md:122-128`](README.md#L122-L128) | `PROTOCOL.md` در layout انبار آمده، ولی هیچ کدی نمی‌سازدش (فقط دستی کپی می‌شود) | layout را با «دستی کپی می‌شود» هم‌خوان کنید |
| F7 | [`docs/roadmap.md:45`](docs/roadmap.md#L45) | معیار پذیرش «از Claude Code و opencode هم خوانده می‌شود» در این مخزن قابل بررسی نیست | علامت «تحقق‌نیافته» بزنید یا حذفش کنید |
| F8 | `docs/operations.md` و README | بعد از اصلاح A–E، بخش «محدودیت‌ها» و «Consolidation» را با رفتار جدید هم‌خوان کنید | در همان commit اصلاح |

## G. ریزها — P3

- `index.line_for`/`render`/`rebuild` پارامتر `prefix` دارند که هیچ‌جا غیرخالی صدایش نمی‌زنند
  ([`memlib/index.py:21`](memlib/index.py#L21))؛ حذفش کنید یا دلیلش را بنویسید.
- `Memory.size` ([`memlib/store.py:185`](memlib/store.py#L185)) هیچ‌جا خوانده نمی‌شود؛
  `store.load` آن را مقدار می‌دهد. یا استفاده کنید یا بردارید.
- هر نوشتن دو بار ایندکس را می‌سازد: `index.rebuild` در
  [`memlib/dream.py:79`](memlib/dream.py#L79) و بعد `_repair_index` در
  [`memlib/dream.py:138`](memlib/dream.py#L138). می‌شود یک بار در پایان ساخت، با
  fallback در مسیر خطا؛ `Stats.index_lines` را هم در همان مسیر یک‌بار حساب کنید.
- `_merge_body` ([`memlib/dream.py:262`](memlib/dream.py#L262)) خطوط خالی را دور می‌ریزد،
  پس پاراگراف‌های بدنهٔ ادغام‌شده به هم می‌چسبند. حفظ فاصله‌ها یا مستندکردن رفتار.
- `originSessionId` در نوشته‌های MCP همیشه خالی است چون `_tool_write` هیچ `session_id`ی
  نمی‌فرستد ([`memlib/mcp.py:391`](memlib/mcp.py#L391)) و ابزار هم آرگومانی برای آن ندارد
  (D5).
- [`memlib/mcp.py:36`](memlib/mcp.py#L36) می‌گوید «Verified against the MCP lifecycle spec»
  بدون ارجاع؛ لینک بدهید یا ادعا را بردارید.
- تست ندارد: سقف ۱۰۰KB، `--session`، traversal در read، حفظ متادیتا، description چندخطی.
  با A/B/E اضافه می‌شوند.

## خارج از محدوده

این‌ها تصمیم بسته‌اند و بازکردنشان فقط با دلیل تازه مجاز است: جست‌وجوی کلیدواژه یا آستانه،
سقف خط برای ایندکس، تثبیت بعد از هر تغییر، نبود git/state/`dream.log`، MCP-only بودن،
نبود فایل تنظیمات، و اینکه `PROTOCOL.md` مسیر onboarding نیست.
همچنین چیزهایی که قبلاً حذف شده‌اند و نباید برگردند: استخراج پایان سشن، تزریق در هر prompt،
و پل symlink به حافظهٔ بومی Claude Code (بک‌اندش مسیر symlink را رد می‌کند) — دلیل‌ها در
design §۹ و §۱۱. تصمیم‌های باز در design §۱۲
(پشتیبان‌گیری راه‌دور، نمایش خودکار `PROTOCOL.md`) هم کار امروز نیستند.

## پایان کار

1. `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v` — همه سبز، با تست‌های تازه.
2. بازتولیدهای A/B/C/E را دوباره اجرا کنید: هر کدام باید رفتار درست بدهد.
3. `git diff` را بخوانید؛ هر تغییر در رفتار باید در README/design/operations منعکس شده باشد.
4. اگر کاربر خواست، `uv tool install --reinstall .` تا ایجنت‌ها کد تازه را ببینند
   (وگرنه `mem` روی PATH همان نسخهٔ قدیمی می‌ماند).
5. این فایل را حذف کنید یا با کارهای باقی‌مانده بازنویسی کنید.
