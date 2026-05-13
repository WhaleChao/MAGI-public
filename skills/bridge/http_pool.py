"""
MAGI 共享 HTTP Session Pool
============================
提供預配置的 requests.Session，所有 bridge 模組共用。
- 連線池化（TCP keep-alive 重用）
- 自動 retry on 502/503
- 預設 timeout
"""

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_POOL_CONNECTIONS = int(os.environ.get("MAGI_HTTP_POOL_CONNECTIONS", "8"))
_POOL_MAXSIZE = int(os.environ.get("MAGI_HTTP_POOL_MAXSIZE", "16"))
_RETRY_TOTAL = int(os.environ.get("MAGI_HTTP_RETRY_TOTAL", "2"))

_retry = Retry(
    total=_RETRY_TOTAL,
    backoff_factor=0.3,
    status_forcelist=[502, 503],
    allowed_methods=["GET", "POST"],
)

_http_adapter = HTTPAdapter(
    pool_connections=_POOL_CONNECTIONS,
    pool_maxsize=_POOL_MAXSIZE,
    max_retries=_retry,
)

_session = requests.Session()
_session.mount("http://", _http_adapter)
_session.mount("https://", HTTPAdapter(
    pool_connections=max(4, _POOL_CONNECTIONS // 2),
    pool_maxsize=max(8, _POOL_MAXSIZE // 2),
    max_retries=_retry,
))


def get_session() -> requests.Session:
    """Return the shared, pool-configured requests.Session."""
    return _session
