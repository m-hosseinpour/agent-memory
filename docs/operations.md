# راه‌اندازی و بهره‌برداری

> وضعیت این ماشین تا ۲۰۲۶-۱۰-۳۰. این سند می‌گوید انبار کجاست، چه ورودی‌هایی دارد،
> چطور به ایجنت‌ها وصل شده و چطور می‌شود بررسی‌اش کرد. طرح در [`design.md`](design.md)
> است و کار باز فعلی در مخزن نیست (قاعده‌اش در [`AGENTS.md`](../AGENTS.md)).

## انبار

- ریشه: `~/.agents/memory` (پیش‌فرض `DEFAULT_ROOT`)، قابل تغییر با `MEM_ROOT` یا `--root`.
- دو scope: `global/` برای یادداشت‌های همه‌جا، و `projects/<slug>/` برای هر دایرکتوری کاری.
  اسلاگ از مسیر مطلق ساخته می‌شود و هر نویسهٔ بیرون از `A-Za-z0-9._-` به `-` تبدیل می‌شود
  (پس `.` و `_` می‌مانند)؛ در ویندوز مسیر پیش از ساخت با `normcase` یکسان می‌شود
  (`C:/Users/u/proj` → `c--users-u-proj`).
- در هر scope یک `MEMORY.md` هست که **تولیدشده** است: هر نوشتن، حذف و `dream` آن را از روی
  فایل‌ها از نو می‌سازد. `mem index` نسخهٔ زندهٔ هر دو scope را چاپ می‌کند. ویرایش دستی
  `MEMORY.md` ماندگار نیست.
- فایل هر یادداشت: frontmatter با `name`، `description` و
  `metadata.{node_type,type,modified}`، سپس بدنه. یک یادداشت در هر فایل،
  نام فایل تخت و با پسوند `.md`؛ `MEMORY.md` (در هر بزرگی‌حروفی) نام مجاز نیست و `mem read`
  هم آن را نمی‌خواند (`mem index` جای آن است).

## مهاجرت از Claude Code

```bash
./bin/mem migrate --dry-run                 # پیش‌نمایش، بدون نوشتن
./bin/mem migrate                           # همهٔ پروژه‌ها، هرکدام در scope خودش
./bin/mem migrate --global user,feedback    # انتقال نوع‌های عمومی به global/
./bin/mem migrate --cwd "$PWD" --from /path/to/memory   # autoMemoryDirectory سفارشی
```

منبع پیش‌فرض `${CLAUDE_CONFIG_DIR:-~/.claude}/projects` است. `MEMORY.md` منبع کپی نمی‌شود
(تولیدشده است) و هر یادداشت از `store.write` رد می‌شود. scope از cwd واقعیِ ثبت‌شده در لاگ
سشن ساخته می‌شود، نه از نام پوشهٔ Claude Code (آن نام نقطه را به `-` تبدیل می‌کند و
برگشت‌پذیر نیست). فایل موجود بازنویسی نمی‌شود: یادداشت یکسان `exists` و متفاوت `collision`
است؛ یادداشتی که تثبیت قبلی زیر نام دیگری نگه داشته هم `exists` می‌شود، پس اجرای دوباره
انبار را تغییر نمی‌دهد. خروجی، فایل‌های ردشده (`refused`/`invalid`/`collision`) را تک‌به‌تک
نشان می‌دهد و تثبیت یک‌بار در پایان هر scope اجرا می‌شود.

## ورودی‌ها

| ورودی | اثر |
|---|---|
| `MEM_ROOT` | ریشهٔ انبار؛ برای تست یا انبار جایگزین. |
| `--root` | همان، فقط برای همان فرمان CLI (روی `MEM_ROOT` می‌نشیند). |
| `MEMORY_PROJECT_CWD` | فقط MCP: اگر درخواست `cwd` نداشته باشد، scope پروژه از این مسیر ساخته می‌شود. |
| `cwd` در درخواست MCP | بر `MEMORY_PROJECT_CWD` اولویت دارد. |
| `cwd`/`--cwd` در CLI | دایرکتوری پروژه برای همان فرمان؛ پیش‌فرض: دایرکتوری جاری. |

در MCP ترتیب این است: `cwd` صریح ← `MEMORY_PROJECT_CWD` ← دایرکتوری جاری سرور.

**هشدار:** سروری که برای چند پروژه یک‌جا اجرا می‌شود (مثل DSH) یک دایرکتوری جاری بی‌معنا
دارد. درخواست‌ها باید `cwd` صریح بفرستند، وگرنه یادداشت در scope پروژهٔ اشتباه می‌نشیند.

## نصب و به‌روزرسانی

```bash
uv tool install .        # mem روی PATH
./bin/mem write ...      # همان کد، از داخل چک‌اوت، بدون نصب
```

ایجنت‌ها `mem` روی `PATH` را صدا می‌زنند، نه فایل داخل چک‌اوت. بعد از هر تغییر در کد:

```bash
uv tool install --reinstall .
```

وگرنه ایجنت‌ها کد قدیمی را اجرا می‌کنند و تست‌های داخل چک‌اوت ممکن است سبز باشند در حالی
که رفتار واقعی ایجنت چیز دیگری است.

## ویندوز

- نصب همان `uv tool install .` است و `mem.exe` روی `PATH` می‌آید؛ `python -m memlib` هم همان
  CLI است. `bin/mem` مخصوص bash است و `bin\mem.cmd` معادل ویندوزیِ بدون نصب.
- قفل scope در ویندوز روی یک بایت از `<scope>/.mem.lock` است، چون ویندوز هندل پوشه را قفل
  نمی‌کند. فایل state نیست: با مرگ پروسه آزاد می‌شود و `MEMORY.md` آن را نمی‌بیند.
- نوشتن همیشه LF است و خواندن CRLF و CR را هم می‌فهمد؛ `memory.md` در هر بزرگی‌حروفی همان
  `MEMORY.md` حساب می‌شود.
- CLI خروجی UTF-8 می‌دهد. در Windows PowerShell 5.1 برای متن غیر‌ASCII بهتر است
  `[Console]::OutputEncoding = [Text.Encoding]::UTF8` یک‌بار ست شود یا از PowerShell 7 استفاده
  شود.

## اتصال ایجنت‌ها

| ایجنت | محل | نکته |
|---|---|---|
| Claude Code | `~/.claude.json`، سرور `mem` در scope کاربر | علاوه بر آن `autoMemoryEnabled: false` در `~/.claude/settings.json`، تا حافظهٔ بومی Claude با این انبار هم‌زمان ننویسد. |
| opencode | `~/.config/opencode/opencode.json` | زیر `mcp.servers`. |
| DSH | `~/.dsh/cordis.patch.yml` | یک ردیف `@deepseek-ai/dsh-mcp-client` با `serverName: mem`. |

قالب دقیق این سه در README آمده است. بررسی:

```bash
claude mcp list                 # mem باید connected باشد
opencode mcp list               # همان؛ بعد از restart سرویس اول opencode debug config
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  | ./bin/mem mcp | python3 -m json.tool | head -40
```

هر سه کلاینت `mem mcp` را بدون env اجرا می‌کنند، پس هر سه همان `~/.agents/memory` را می‌بینند.
خواندن بین دو کلاینت در ۲۰۲۶-۱۰-۲۹ تأیید شد: یادداشتی که از راه `mem mcp` روی یک انبار موقت
نوشته شد، با `claude -p` (ابزار `mcp__mem__memory_read`) و `opencode run` (ابزار
`mem_memory_read`) هم خوانده شد.

`opencode mcp list` بلافاصله بعد از `opencode service restart` ممکن است «No MCP servers
configured» بگوید تا وقتی location بارگذاری شود؛ یک `opencode debug config` پیش از آن کافی است.

## بررسی سریع

```bash
export MEM_ROOT=$(mktemp -d)
export PROJ=$(mktemp -d)
./bin/mem write --scope project --cwd "$PROJ" --path a.md --name shared \
  --description "a note" --if-version new --content "body"
./bin/mem write --scope project --cwd "$PROJ" --path b.md --name shared \
  --description "a note" --if-version new --content "body"
ls "$MEM_ROOT"/projects/*/          # a.md رفته است: تثبیت همان لحظه اجرا شد
./bin/mem index --cwd "$PROJ"
./bin/mem read --scope project --cwd "$PROJ" --path b.md
rm -rf "$MEM_ROOT" "$PROJ"
```

در ویندوز همین بررسی با `mem` (یا `python -m memlib`) و `$env:MEM_ROOT` / `$env:PROJ` انجام
می‌شود.

## محدودیت‌های شناخته‌شده

- ادغام فقط با «واژه‌های یکسان» در `description` رخ می‌دهد (Jaccard ≥ ۰٫۹۵ تا ۱۹ واژهٔ متمایز،
  که در این اندازه فقط با همان مجموعهٔ واژه به دست می‌آید)، نه با شباهت جزئی: افزودن یک قید
  کوتاه (`... tag blue`) یادداشت را حذف نمی‌کند و بازگویی با واژه‌های دیگر یادداشت جداگانه می‌ماند.
  توصیف با بیش از ۱۹ واژهٔ متمایز هرگز با شباهت ادغام نمی‌شود، پس دو یادداشت تکراری با توصیف
  بلند کنار هم می‌مانند؛ «تغییر نام + تناقض» هم تشخیص داده نمی‌شود.
- MCP ابزار حذف ندارد؛ حذف از راه CLI (`mem forget`) است.
- `mem read` فقط نام تخت `.md` می‌پذیرد؛ `..`، مسیر مطلق و `MEMORY.md` (در هر بزرگی‌حروفی)
  رد می‌شوند. بایت نامعتبر UTF-8 در فایل دست‌نویس با `U+FFFD` جایگزین می‌شود، نه تراک‌بک.
- `mem index` فقط چاپ می‌کند؛ فایل `MEMORY.md` فقط با نوشتن یا `dream` بازنویسی می‌شود.
- بدون تاریخچهٔ git و بدون استیت: حذف اشتباه فقط با بازنویسی دستی جبران می‌شود.
- انبار فقط روی همین ماشین است و خودش پشتیبان نمی‌گیرد: خرابی دیسک یعنی از دست رفتن حافظه. اگر
  پشتیبان می‌خواهید، بیرون از ابزار از `~/.agents/memory` کپی دوره‌ای بگیرید (تصمیمش در
  design §۱۱).
- یک سرور MCP برای چند پروژه باید در هر درخواست `cwd` بگیرد (بالا).
- `PROTOCOL.md` در ریشهٔ انبار خودکار ساخته نمی‌شود؛ اگر لازم است، دستی از مخزن کپی کنید.
- فرانت‌متر فقط سه کلیدِ تحت مدیریت را نگه می‌دارد (`name`، `description`، `metadata`)؛ کلید
  دست‌نویس دیگری مثل `title` در اولین بازنویسی حذف می‌شود. عنوانِ ایندکس از `name` ساخته
  می‌شود.
- `name` و `description` باید یک‌خطی باشند: هر نویسهٔ خط‌شکن (newline، carriage return،
  U+2028 و مانند آن) پیش از نوشتن با خطای `invalid` رد می‌شود و نه فایلی ساخته می‌شود نه scope
  تازه‌ای. متن چندخطی جای‌اش بدنه است.

کار باز فعلی وجود ندارد؛ اگر کاری باز شود، طبق [`AGENTS.md`](../AGENTS.md) در `HANDOFF.md`
نوشته می‌شود.
