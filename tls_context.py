"""项目统一的严格 TLS 上下文。"""

import ssl


def create_verified_context() -> ssl.SSLContext:
    """优先使用 certifi CA，缺失时回退系统 CA；始终验证证书和主机名。"""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
