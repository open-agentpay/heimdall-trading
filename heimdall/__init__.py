"""
Heimdall Trading Platform
Production-Grade Multi-Agent AI Quant Trading & Asset Management Platform.
"""

import sys
import logging

# 默认全局基础日志输出，避免无 handlers 时 INFO 级别日志被静默吞没
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

__version__ = "0.1.0"
