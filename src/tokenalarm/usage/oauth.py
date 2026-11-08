"""OPT-IN fallback: the undocumented ``GET https://api.anthropic.com/api/oauth/usage`` endpoint.

Disabled by default (``usage.oauth_endpoint: false``). It reads the access token that Claude Code stores in
``~/.claude/.credentials.json`` (Windows/Linux). It is not an official API, its format may change, it is
rate-limited, and Anthropic's terms discourage tools that handle Claude.ai credentials — use it only for
your own machine and only if the official sources do not work for you. The token is never written anywhere.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from ..paths import claude_home
from .base import Snapshot
from .parse import parse_oauth_usage

log = logging.getLogger(__name__)
URL = "https://api.anthropic.com/api/oauth/usage"


class OAuthUnavailable(RuntimeError):
    pass


def _token() -> str:
    path = claude_home() / ".credentials.json"
    if not path.exists():
        raise OAuthUnavailable(f"{path} 없음 (macOS 키체인 저장 방식은 지원하지 않음)")
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise OAuthUnavailable("claudeAiOauth.accessToken 없음")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 < time.time():
        raise OAuthUnavailable("토큰 만료됨 — Claude Code를 한 번 실행하면 갱신됩니다")
    return token


def fetch_oauth_usage(timeout: float = 15) -> Snapshot | None:
    req = urllib.request.Request(URL, headers={
        "Authorization": f"Bearer {_token()}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "tokenalarm",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log.warning("oauth usage HTTP %s", e.code)
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("oauth usage failed: %s", e)
        return None
    return parse_oauth_usage(data, time.time())
