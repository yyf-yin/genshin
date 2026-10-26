"""B 站攻略视频取材。移植自 dsh-bili-whale（MIT），见 THIRD_PARTY_NOTICES.md。"""

from .client import BiliClient, BiliError, Credentials, RateLimited, load_credentials

__all__ = ["BiliClient", "BiliError", "Credentials", "RateLimited", "load_credentials"]
