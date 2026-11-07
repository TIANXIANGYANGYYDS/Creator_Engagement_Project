"""Opt-in lease admission budgets; these are not platform access guarantees."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProxyRequirements:
    platform: str = ""
    min_remaining_seconds: float = 0
    exclusive: bool = False


class ProxyFailure(RuntimeError):
    def __init__(self, reason: str, *, kind: Literal["transport", "platform", "task"], platform: str = ""):
        super().__init__(reason)
        self.kind = kind
        self.platform = platform


def collection_requirements(platform: str, *, browser: bool = False, author: bool = False) -> ProxyRequirements:
    # Historical operation P95 includes queueing/retries. Use coarse, conservative
    # admission classes instead of treating it as a single-request timeout.
    if browser:
        return ProxyRequirements(platform, 90, True)
    if author:
        return ProxyRequirements(platform, 60, True)
    if platform == "douyin":
        # The visitor factory upgrades to 70 s before a cold browser bootstrap.
        return ProxyRequirements(platform, 25, True)
    seconds = {"weibo": 40, "bilibili": 60, "kuaishou": 60, "wechat": 60}.get(platform, 35)
    return ProxyRequirements(platform, seconds)
