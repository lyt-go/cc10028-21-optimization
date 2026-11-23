"""业务路由汇总：固定业务路径在前，通用资源路由在后。

通用资源路由含 /api/{module}/{entry_id}，必须先注册固定路径
（如 /api/doc_version/compare），否则会被它抢先匹配。
"""
from __future__ import annotations

from app.routers import resource, versioning

ROUTERS = [versioning, resource]
