# -*- coding: utf-8 -*-
"""AstrBot 插件中枢标准联动桥接模块（零强依赖：未安装 astrbot_plugin_hub 时静默空操作）。"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

HUB_PROTOCOL_VERSION = 1
HUB_BUS_KEY = "_astrbot_plugin_hub_bus"


def get_hub_bus(context: Any = None) -> Any:
    """运行时软探测中枢事件总线：先查全局总线槽，再查 context 中注册的 hub 插件。"""
    bus = sys.modules.get(HUB_BUS_KEY)
    if bus is not None and hasattr(bus, "emit"):
        return bus
    if context is not None and hasattr(context, "get_registered_star"):
        try:
            meta = context.get_registered_star("astrbot_plugin_hub")
            inst = getattr(meta, "star_cls", meta) if meta is not None else None
            if inst is not None and hasattr(inst, "bus"):
                return inst.bus
        except Exception:
            pass
    return None


def emit_hub_event(
    source: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    context: Any = None,
) -> bool:
    """安全上报跨插件事件（兼容同步与异步上下文，未安装中枢或异常时绝不影响主流程）。"""
    bus = get_hub_bus(context)
    if bus is None:
        return False
    data = dict(payload or {})
    try:
        res = bus.emit(source=source, event_type=event_type, payload=data)
        if asyncio.iscoroutine(res):
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(res)
            except RuntimeError:
                res.close()
                return False
        return True
    except Exception:
        return False
