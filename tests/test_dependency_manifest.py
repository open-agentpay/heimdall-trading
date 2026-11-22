"""
================================================================================
回归测试：2026-11-21 代码审计中危问题 M3 —— 依赖清单一致性
================================================================================
防止依赖声明再次与代码实际导入脱节：
1. heimdall/ 中每个第三方 import 都必须在 pyproject.toml 的运行时依赖中登记；
2. pyproject.toml 中登记的每个运行时依赖都必须被 heimdall/ 实际使用（不允许死依赖）；
3. requirements.txt 与 pyproject.toml 运行时依赖逐条一致（含版本约束与 extras）；
4. pytest 等测试工具只能出现在 dev 分组，不得混入运行时依赖；
5. [project.scripts] 指向的命令行入口函数真实存在。
"""

import ast
import re
import sys
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = pytest.importorskip("tomli")

ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = ROOT / "heimdall"

# import 顶层模块名 -> PyPI 发行包名（规范化小写、短横线）
IMPORT_TO_DIST = {
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "socksio": "httpx",          # 由 httpx[socks] extra 提供
    "aiohttp": "aiohttp",
    "asyncpg": "asyncpg",
    "openai": "openai",
    "anthropic": "anthropic",
    "binance": "binance-futures-connector",
    "eth_account": "eth-account",
    "eth_utils": "eth-utils",
    "msgpack": "msgpack",
    "Crypto": "pycryptodome",
}

TEST_ONLY_DISTS = {"pytest", "pytest-asyncio", "black", "ruff", "mypy"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_name(spec: str) -> str:
    return _norm(re.split(r"[\[<>=!~;\s]", spec.strip(), maxsplit=1)[0])


def _norm_spec(spec: str) -> str:
    return re.sub(r"\s+", "", spec).lower()


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirements(path: Path) -> list:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _third_party_imports() -> dict:
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    found = {}
    for py in PKG_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top == "heimdall" or top in stdlib:
                    continue
                found.setdefault(top, set()).add(str(py.relative_to(ROOT)))
    return found


def test_every_third_party_import_is_declared():
    declared = {_dist_name(s) for s in _pyproject()["project"]["dependencies"]}
    problems = []
    for top, files in sorted(_third_party_imports().items()):
        dist = IMPORT_TO_DIST.get(top)
        if dist is None:
            problems.append(f"{top}（{sorted(files)}）未在 IMPORT_TO_DIST 映射中登记")
        elif _norm(dist) not in declared:
            problems.append(f"{top} -> {dist}（{sorted(files)}）未在 pyproject.toml 中声明")
    assert not problems, "存在未声明的第三方依赖：\n" + "\n".join(problems)


def test_no_unused_runtime_dependencies():
    used = {_norm(IMPORT_TO_DIST[t]) for t in _third_party_imports() if t in IMPORT_TO_DIST}
    declared = {_dist_name(s) for s in _pyproject()["project"]["dependencies"]}
    unused = sorted(declared - used)
    assert not unused, f"pyproject.toml 中存在 heimdall/ 未使用的运行时依赖: {unused}"


def test_requirements_txt_matches_pyproject():
    py_specs = {_norm_spec(s) for s in _pyproject()["project"]["dependencies"]}
    req_specs = {_norm_spec(s) for s in _requirements(ROOT / "requirements.txt")}
    assert req_specs == py_specs, (
        f"requirements.txt 与 pyproject.toml 不一致\n"
        f"  仅在 requirements.txt: {sorted(req_specs - py_specs)}\n"
        f"  仅在 pyproject.toml: {sorted(py_specs - req_specs)}"
    )


def test_test_tools_only_in_dev_group():
    data = _pyproject()
    runtime = {_dist_name(s) for s in data["project"]["dependencies"]}
    runtime |= {_dist_name(s) for s in _requirements(ROOT / "requirements.txt")}
    leaked = sorted(runtime & TEST_ONLY_DISTS)
    assert not leaked, f"测试/开发工具不应出现在运行时依赖中: {leaked}"

    dev = {_dist_name(s) for s in data["project"]["optional-dependencies"]["dev"]}
    assert {"pytest", "pytest-asyncio"} <= dev
    dev_req = {_dist_name(s) for s in _requirements(ROOT / "requirements-dev.txt")}
    assert dev == dev_req, f"requirements-dev.txt 与 [dev] 分组不一致: {sorted(dev ^ dev_req)}"


def test_console_script_entry_point_exists():
    for name, target in _pyproject()["project"]["scripts"].items():
        module, func = target.split(":")
        path = ROOT / (module.replace(".", "/") + ".py")
        assert path.exists(), f"脚本 {name} 指向的模块 {module} 不存在"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        } | {
            t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)
        }
        assert func in names, f"脚本 {name} 指向的 {target} 在 {path.name} 中不存在"


def test_wheel_packages_declared():
    pkgs = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert "heimdall" in pkgs
