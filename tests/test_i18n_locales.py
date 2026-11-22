"""
================================================================================
回归测试：前端多语言（i18next + react-i18next）
================================================================================
检查逻辑见 tests/i18n_static_check.py：
1. 每个命名空间在 zh-CN / en-US 都有，键、插值参数、<Trans> 标签一致，英文包里没有中文；
2. 源码里用到的每个键在语言包里都存在；
3. 源码里没有写死的中文界面文字（注释、console、判断后端中文报错的 .includes 除外）。
4. 依赖与构建环境：package.json 声明了 i18next 三件套，前端镜像用 Node 22。
"""
import json
from pathlib import Path

import pytest

from tests.i18n_static_check import ROOT, check_locales, check_source, load_locales, source_files

WEB = ROOT / "web"


def test_locales_consistent():
    problems = check_locales()
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(WEB / "src")))
def test_source_file_i18n(path):
    problems = check_source(path, load_locales())
    assert problems == [], "\n".join(problems)


def test_dependencies_and_node_version():
    deps = json.loads((WEB / "package.json").read_text(encoding="utf-8"))["dependencies"]
    for name in ("i18next", "react-i18next", "i18next-browser-languagedetector"):
        assert name in deps, f"package.json 缺少 {name}"
    assert "FROM node:22" in (WEB / "Dockerfile").read_text(encoding="utf-8"), "i18next 27 需要 Node.js 22"
    assert "import './i18n';" in (WEB / "src" / "main.jsx").read_text(encoding="utf-8")
