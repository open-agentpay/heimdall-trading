"""
Heimdall 交易平台 - API 模块导出文件
包含 FastAPI Web 应用实例 app 与机器人管理器 bot_manager。
"""

def __getattr__(name: str):
    if name in ("app", "bot_manager"):
        from heimdall.api.server import app, bot_manager
        return app if name == "app" else bot_manager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["app", "bot_manager"]
