"""
server.py 拆分回归测试（2026-11-22）

覆盖：
1. 路由表：25 条业务路径（方法 + 路径）全部挂载，且全应用内不存在两条可能匹配同一 URL 的路由；
2. 兼容导出：from heimdall.api.server import 的端点函数、请求模型、辅助函数与路由模块中的是同一个对象；
3. 单例：server.bot_manager / instance_lock 与 heimdall.api.runtime 中的是同一个对象；
4. 日志：路由模块 logger 都挂在 heimdall.api.server 之下；
5. 架构约束：路由模块不 import heimdall.api.server；业务路由模块之间只允许依赖 common；
   routes 目录下新增模块必须登记到 ROUTERS（common 除外）。
"""

import ast
import logging
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

import heimdall.api.server as server
from heimdall.api import runtime
from heimdall.api.routes import ROUTERS, analytics, bot_control, bots, common, market, risk, stream, system, trading

ROUTES_DIR = Path(__file__).resolve().parents[1] / "heimdall" / "api" / "routes"

#: 拆分前 server.py 中定义的全部业务路径
EXPECTED = {
    ("GET", "/health"),
    ("GET", "/api/v1/bots"),
    ("POST", "/api/v1/bots"),
    ("PATCH", "/api/v1/bots/{bot_id}/capital"),
    ("GET", "/api/v1/portfolio/capital-distribution"),
    ("POST", "/api/v1/bots/{bot_id}/start"),
    ("POST", "/api/v1/bots/{bot_id}/stop"),
    ("DELETE", "/api/v1/bots/{bot_id}"),
    ("POST", "/api/v1/bots/{bot_id}/step"),
    ("GET", "/api/v1/bots/{bot_id}/latest-state"),
    ("GET", "/api/v1/scheduler/jobs"),
    ("GET", "/api/v1/bots/{bot_id}/nav"),
    ("GET", "/api/v1/bots/{bot_id}/decisions"),
    ("GET", "/api/v1/bots/{bot_id}/trades"),
    ("POST", "/api/v1/bots/{bot_id}/orders/paper"),
    ("GET", "/api/v1/bots/{bot_id}/orders/paper/quote"),
    ("POST", "/api/v1/emergency/kill"),
    ("GET", "/api/v1/market/klines"),
    ("GET", "/v1/klines"),
    ("GET", "/api/v1/market/funding"),
    ("GET", "/v1/funding"),
    ("GET", "/api/v1/market/guard"),
    ("GET", "/v1/guard/status"),
    ("GET", "/api/v1/market/radar"),
    ("WS", "/ws/stream"),
}


def _flatten_routes(routes, prefix=""):
    """
    展开应用的路由表，返回 (路径前缀, 路由) 序列。

    FastAPI 0.165 起 include_router() 不再把子路由逐条复制进 app.routes，而是放入一个惰性的
    ``_IncludedRouter``（带 ``original_router`` 与 ``include_context``），请求时才展开；
    旧版本则直接是 APIRoute。这里按属性识别（不依赖 FastAPI 私有类），两种版本都能正确展开，
    嵌套 include 的前缀逐层累加。
    """
    for r in routes:
        inner = getattr(r, "original_router", None)
        ctx = getattr(r, "include_context", None)
        if inner is not None and ctx is not None:
            yield from _flatten_routes(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        yield prefix, r


def _app_routes():
    out = []
    for prefix, r in _flatten_routes(server.app.routes):
        if isinstance(r, APIRoute):
            for m in r.methods:
                out.append((m, prefix + r.path))
        elif isinstance(r, WebSocketRoute):
            out.append(("WS", prefix + r.path))
    return out


def test_route_table_is_not_empty():
    """防止展开逻辑失效时其余路由测试"空跑通过"（例如 FastAPI 再次改变 app.routes 的结构）"""
    routes = _app_routes()
    assert len(routes) >= 47, f"只展开出 {len(routes)} 条路由，请检查 _flatten_routes 是否适配当前 FastAPI 版本"


# -----------------------------------------------------------------------------
# 1. 路由表
# -----------------------------------------------------------------------------

def test_all_business_routes_mounted_once():
    routes = _app_routes()
    assert EXPECTED <= set(routes), EXPECTED - set(routes)
    assert len(routes) == len(set(routes)), "存在重复挂载的路由"
    business = sum(len(r.routes) for _, r in ROUTERS)
    assert business == len(EXPECTED) == 25


def _may_overlap(a: str, b: str) -> bool:
    sa, sb = a.strip("/").split("/"), b.strip("/").split("/")
    return len(sa) == len(sb) and all(x == y or x.startswith("{") or y.startswith("{") for x, y in zip(sa, sb))


def test_no_two_routes_can_match_the_same_url():
    """保证挂载顺序不影响路由匹配（拆分只改变了挂载顺序）"""
    routes = _app_routes()
    for i, (m1, p1) in enumerate(routes):
        for m2, p2 in routes[i + 1:]:
            if m1 == m2 and p1 != p2:
                assert not _may_overlap(p1, p2), f"{m1} {p1} 与 {p2} 可能匹配同一 URL"


# -----------------------------------------------------------------------------
# 2 & 3. 兼容导出与单例
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("module, name", [
    (system, "health_check"),
    (bots, "CreateBotRequest"), (bots, "UpdateCapitalRequest"), (bots, "list_bots"), (bots, "create_bot"),
    (bots, "update_bot_capital"), (bots, "get_portfolio_capital_distribution"), (bots, "delete_bot"),
    (bots, "get_bot_latest_state"),
    (bot_control, "StartBotRequest"), (bot_control, "start_bot"), (bot_control, "stop_bot"),
    (bot_control, "trigger_step"), (bot_control, "list_scheduler_jobs"),
    (analytics, "get_bot_nav"), (analytics, "get_bot_decisions"), (analytics, "get_bot_trades"),
    (trading, "PaperOrderRequest"), (trading, "place_paper_order"), (trading, "get_paper_order_quote"),
    (trading, "_paper_price_max_deviation_pct"), (trading, "_fetch_guard_status_fail_closed"),
    (trading, "_resolve_live_reference_price"),
    (risk, "emergency_kill"), (risk, "get_risk_radar_telemetry"),
    (market, "get_market_klines"), (market, "get_market_funding"), (market, "get_cross_exchange_guard_status"),
    (stream, "websocket_stream_endpoint"),
    (common, "_assert_bot_readable"),
])
def test_server_reexports_same_objects(module, name):
    assert getattr(server, name) is getattr(module, name)


def test_singletons_are_shared():
    assert server.bot_manager is runtime.bot_manager
    assert server.instance_lock is runtime.instance_lock
    for mod in (bots, bot_control, analytics, trading, risk, market):
        assert mod.bot_manager is runtime.bot_manager


# -----------------------------------------------------------------------------
# 4. 日志
# -----------------------------------------------------------------------------

def test_route_loggers_are_children_of_server_logger():
    for mod in (system, bots, bot_control, analytics, trading, risk, market, stream, common):
        lg = getattr(mod, "logger")
        assert isinstance(lg, logging.Logger) and lg.name.startswith("heimdall.api.server."), lg.name


# -----------------------------------------------------------------------------
# 5. 架构约束
# -----------------------------------------------------------------------------

def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_route_modules_dependency_rules():
    prefix = "heimdall.api.routes."
    for path in ROUTES_DIR.glob("*.py"):
        mods = list(_imports(path))
        assert "heimdall.api.server" not in mods, f"{path.name} 不应 import heimdall.api.server"
        if path.stem == "__init__":
            continue
        deps = {m[len(prefix):].split(".")[0] for m in mods if m.startswith(prefix)}
        assert deps <= {"common"}, f"{path.stem} 只能依赖 common，实际依赖 {deps}"


def test_every_route_module_is_registered():
    registered = {name for name, _ in ROUTERS}
    on_disk = {p.stem for p in ROUTES_DIR.glob("*.py")} - {"__init__", "common"}
    assert on_disk == registered, f"未登记到 ROUTERS 的路由模块: {on_disk ^ registered}"
