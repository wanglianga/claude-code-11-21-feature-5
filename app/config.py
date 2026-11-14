"""运行时配置：仅从环境变量读取，默认指向容器内开发数据库。"""
from __future__ import annotations

import os

DEFAULT_DATABASE_URL = "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
