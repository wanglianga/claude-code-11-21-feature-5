"""跨模块共享的业务异常（独立模块以避免 services <-> layers 循环导入）。"""
from __future__ import annotations


class DomainError(Exception):
    """业务错误，message 面向调用方，status 为 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status
