# راه‌اندازی و بهره‌برداری

> وضعیت این ماشین تا ۲۰۲۶-۱۰-۲۸. این سند می‌گوید انبار کجاست، چه ورودی‌هایی دارد،
> چطور به ایجنت‌ها وصل شده و چطور می‌شود بررسی‌اش کرد. طرح در [`design.md`](design.md)
> است و کارهای باز در [`HANDOFF.md`](../HANDOFF.md).

## انبار

- ریشه: `~/.agents/memory` (پیش‌فرض `DEFAULT_ROOT`)، قابل تغییر با `MEM_ROOT` یا `--root`.
- دو scope: `global/` برای یادداشت‌های همه‌جا، و `projects/<slug>/` برای هر دایرکتوری کاری.
  اسلاگ از مسیر مطلق ساخته می‌شود و هر نویسهٔ بیرون از `A-Za-z0-9._-` به `-` تبدیل می‌شود
  (پس `.` و `_` می‌مانند).
- در هر scope یک `MEMORY.md` هست که **تولیدشده** است: هر نوشتن، حذف و `dream` آن را از روی
  فایل‌ها از نو می‌سازد. `mem index` نسخهٔ زندهٔ هر دو scope را چاپ می‌کند. ویرایش دستی
  `MEMORY.md` ماندگار نیست.
- فایل هر یادداشت: frontmatter با `name`، `description` و
  `metadata.{node_type,type,originSessionId,modified}`، سپس بدنه. یک یادداشت در هر فایل،
  نام فایل تخت و با پسوند `.md`؛ `MEMORY.md` نام مجاز نیست و `mem read` هم آن را نمی‌خواند
  (`mem index` جای آن است).

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

## اتصال ایجنت‌ها

| ایجنت | محل | نکته |
|---|---|---|
| Claude Code | `~/.claude.json`، سرور `mem` در scope کاربر | علاوه بر آن `autoMemoryEnabled: false` در `~/.claude/settings.json`، تا حافظهٔ بومی Claude با این انبار هم‌زمان ننویسد. |
| opencode | `~/.config/opencode/opencode.json` | زیر `mcp.servers`. |
| DSH | `~/.dsh/cordis.patch.yml` | یک ردیف `@deepseek-ai/dsh-mcp-client` با `serverName: mem`. |

قالب دقیق این سه در README آمده است. بررسی:

```bash
claude mcp list                 # mem باید connected باشد
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  | ./bin/mem mcp | python3 -m json.tool | head -40
```

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

## محدودیت‌های شناخته‌شده

- ادغام فقط با «واژه‌های یکسان» در `description` رخ می‌دهد (Jaccard ≥ ۰٫۹۵ تا ۱۹ واژهٔ متمایز،
  که در این اندازه فقط با همان مجموعهٔ واژه به دست می‌آید)، نه با شباهت جزئی: افزودن یک قید
  کوتاه (`... tag blue`) یادداشت را حذف نمی‌کند و بازگویی با واژه‌های دیگر یادداشت جداگانه می‌ماند.
  توصیف بلندتر از یک خط (بیش از ۱۹ واژه) هرگز با شباهت ادغام نمی‌شود، پس دو یادداشت تکراری با
  توصیف بلند کنار هم می‌مانند؛ «تغییر نام + تناقض» هم تشخیص داده نمی‌شود.
- MCP ابزار حذف ندارد؛ حذف از راه CLI (`mem forget`) است.
- `mem read` فقط نام تخت `.md` می‌پذیرد؛ `..`، مسیر مطلق و `MEMORY.md` رد می‌شوند. بایت
  نامعتبر UTF-8 در فایل دست‌نویس با `U+FFFD` جایگزین می‌شود، نه تراک‌بک.
- `mem index` فقط چاپ می‌کند؛ فایل `MEMORY.md` فقط با نوشتن یا `dream` بازنویسی می‌شود.
- بدون تاریخچهٔ git و بدون استیت: حذف اشتباه فقط با بازنویسی دستی جبران می‌شود.
- یک سرور MCP برای چند پروژه باید در هر درخواست `cwd` بگیرد (بالا).
- `PROTOCOL.md` در ریشهٔ انبار خودکار ساخته نمی‌شود؛ اگر لازم است، دستی از مخزن کپی کنید.
- فرانت‌متر فقط سه کلیدِ تحت مدیریت را نگه می‌دارد (`name`، `description`، `metadata`)؛ کلید
  دست‌نویس دیگری مثل `title` در اولین بازنویسی حذف می‌شود. عنوانِ ایندکس از `name` ساخته
  می‌شود.

ایرادهای باز که هنوز برطرف نشده‌اند (`description` چندخطی و بقیه) در
[`HANDOFF.md`](../HANDOFF.md) فهرست شده‌اند.
