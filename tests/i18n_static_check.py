"""
前端多语言静态检查（不依赖第三方库，pytest 与命令行都能用）

  python3 tests/i18n_static_check.py                      # 检查全部（跳过 NOT_YET_MIGRATED）
  python3 tests/i18n_static_check.py web/src/App.jsx ...  # 只检查指定文件

检查项：
1. 每个命名空间在 zh-CN 与 en-US 两个目录都有，键完全一致，同一个键的插值参数 {{name}} 一致，
   <Trans> 用到的标签（如 <strong>）一致；
2. 英文语言包里没有中文（语言名称例外）；
3. 源码里 t('键') / t('命名空间:键') / i18nKey="键" 用到的键在语言包里都存在；
   不带命名空间的键归属该文件 useTranslation('命名空间') 的第一个命名空间，没有则为 common；
   复数键（键_one / 键_other）也算存在；
4. 源码里没有写死的中文界面文字。以下不算：注释、console.*、判断后端中文报错的 .includes('…')、
   行尾带 // i18n-ignore 的行。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_SRC = ROOT / "web" / "src"
LOCALE_DIR = WEB_SRC / "i18n" / "locales"
LOCALES = ("zh-CN", "en-US")

# 尚未接入多语言的文件（相对 web/src）。接入完成后从这里删掉。
NOT_YET_MIGRATED: set[str] = set()

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
PARAM_RE = re.compile(r"\{\{\s*(\w+)")
TAG_RE = re.compile(r"</?(\w+)\s*/?>")
T_CALL_RE = re.compile(r"""(?<![\w$.])(?:t|i18n\.t)\(\s*['"]([^'"]+)['"]""")
I18NKEY_RE = re.compile(r"""i18nKey=\{?\s*['"]([^'"]+)['"]""")
LABELKEY_RE = re.compile(r"""(?:labelKey|titleKey|msgKey|textKey)\s*:\s*['"]([^'"]+)['"]""")
USE_NS_RE = re.compile(r"""useTranslation\(\s*\[?\s*['"](\w+)['"]""")
EN_CJK_ALLOWED = {"common:language.names.zh-CN"}


def _flatten(node, prefix=""):
    out = {}
    for key, value in node.items():
        full = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(_flatten(value, full))
        else:
            out[full] = value
    return out


def load_locales():
    data = {}
    for lng in LOCALES:
        d = LOCALE_DIR / lng
        data[lng] = {}
        for f in sorted(d.glob("*.json")) if d.is_dir() else []:
            data[lng][f.stem] = _flatten(json.loads(f.read_text(encoding="utf-8")))
    return data


def check_locales(data=None):
    data = data or load_locales()
    problems = []
    zh, en = data["zh-CN"], data["en-US"]
    if not zh:
        problems.append("没有找到 zh-CN 语言包")
    for ns in sorted(set(zh) | set(en)):
        if ns not in zh or ns not in en:
            problems.append(f"命名空间 {ns} 只在一个语言里存在")
            continue
        for key in sorted(set(zh[ns]) - set(en[ns])):
            problems.append(f"en-US 缺少 {ns}:{key}")
        for key in sorted(set(en[ns]) - set(zh[ns])):
            problems.append(f"zh-CN 缺少 {ns}:{key}")
        for key in sorted(set(zh[ns]) & set(en[ns])):
            a, b = zh[ns][key], en[ns][key]
            if not isinstance(a, str) or not isinstance(b, str) or not a.strip() or not b.strip():
                problems.append(f"{ns}:{key} 不是非空字符串")
                continue
            if set(PARAM_RE.findall(a)) != set(PARAM_RE.findall(b)):
                problems.append(f"{ns}:{key} 两种语言的插值参数不一致: {a!r} / {b!r}")
            if sorted(TAG_RE.findall(a)) != sorted(TAG_RE.findall(b)):
                problems.append(f"{ns}:{key} 两种语言的标签不一致: {a!r} / {b!r}")
            if CJK_RE.search(b) and f"{ns}:{key}" not in EN_CJK_ALLOWED:
                problems.append(f"en-US {ns}:{key} 含中文: {b!r}")
    return problems


def _key_exists(zh, ns, key):
    keys = zh.get(ns, {})
    return key in keys or f"{key}_other" in keys or f"{key}_one" in keys


def _strip_comments(text):
    return re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)


def check_source(path: Path, data=None):
    data = data or load_locales()
    zh = data["zh-CN"]
    rel = path.relative_to(ROOT) if path.is_absolute() else path
    text = path.read_text(encoding="utf-8")
    problems = []
    m = USE_NS_RE.search(text)
    default_ns = m.group(1) if m else "common"
    for key in T_CALL_RE.findall(text) + I18NKEY_RE.findall(text) + LABELKEY_RE.findall(text):
        if "${" in key:
            continue
        ns, k = key.split(":", 1) if ":" in key else (default_ns, key)
        if not _key_exists(zh, ns, k):
            problems.append(f"{rel}: 语言包里没有 {ns}:{k}")
    for no, line in enumerate(_strip_comments(text).splitlines(), 1):
        if "i18n-ignore" in line:
            continue
        code = re.sub(r"(?<![:'\"\w])//.*", "", line)
        if not CJK_RE.search(code) or "console." in code or ".includes(" in code:
            continue
        problems.append(f"{rel}:{no}: 写死的中文: {line.strip()[:120]}")
    return problems


def source_files():
    files = []
    for p in sorted(WEB_SRC.rglob("*")):
        if p.suffix not in (".js", ".jsx") or "locales" in p.parts:
            continue
        if str(p.relative_to(WEB_SRC)) in NOT_YET_MIGRATED:
            continue
        files.append(p)
    return files


def check(paths=None):
    data = load_locales()
    problems = check_locales(data)
    targets = [Path(p) if Path(p).is_absolute() else ROOT / p for p in paths] if paths else source_files()
    for p in targets:
        problems += check_source(p, data)
    return problems


if __name__ == "__main__":
    result = check(sys.argv[1:] or None)
    print("\n".join(result) if result else "OK")
    sys.exit(1 if result else 0)
