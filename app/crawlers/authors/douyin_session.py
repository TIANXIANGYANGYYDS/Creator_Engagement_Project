"""Douyin public requests signed with one browser's current visitor identity.

WebSign adapted from Evil0ctal/Douyin_TikTok_Download_API at
9fa3e5406694c80ec0a97399f410948f130d0f9e, Apache-2.0.
See third_party/douyin_download_api/{LICENSE,NOTICE}.
"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import quote, urlencode

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.douyin_abogus_v2 import ABogus, DEFAULT_BROWSER_INFO


VISITOR_COOKIES = ("uifid", "uifid_temp", "uifidtemp", "UIFID", "UIFID_TEMP", "UIFIDTEMP")
SIGNATURE_FIELDS = {"a_bogus", "verifyFp", "fp", "uifid", "timestamp", "x-secsdk-web-signature"}
WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"

# The native fetch is kept before platform scripts wrap it. Only explicitly
# marked probes are intercepted; unrelated page requests always proceed.
INIT_SCRIPT = """(() => {
  const original = window.fetch;
  const originalOpen = XMLHttpRequest.prototype.open;
  const state = {target: null, signed: null, nativeFetch: original.bind(window)};
  window.__creatorDouyin = state;
  window.fetch = function(input, options) {
    const raw = typeof input === 'string' ? input : (input.url || String(input));
    if (state.target && new URL(raw, location.href).searchParams.get('ce_probe') === state.target) {
      state.signed = raw;
      return Promise.reject(new DOMException('signature probe', 'AbortError'));
    }
    return original.apply(this, arguments);
  };
  XMLHttpRequest.prototype.open = function(method, raw) {
    if (state.target && new URL(raw, location.href).searchParams.get('ce_probe') === state.target) {
      state.signed = raw;
      throw new DOMException('signature probe', 'AbortError');
    }
    return originalOpen.apply(this, arguments);
  };
})();"""

PROBE_SCRIPT = """async () => {
  const state = window.__creatorDouyin;
  if (!state) return [];
  state.target = String(Date.now()); state.signed = null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 1000);
  try { await fetch(location.origin + '/aweme/v1/web/aweme/post/?aid=6383&ce_probe=' + state.target,
    {signal: controller.signal}); }
  catch (_) {} finally { state.target = null; clearTimeout(timer); }
  return state.signed ? Array.from(new URL(state.signed, location.href).searchParams.keys()) : [];
}"""

PROFILE_SCRIPT = r"""() => {
  const ua = navigator.userAgent;
  const firefox = ua.match(/Firefox\/([\d.]+)/);
  const chrome = ua.match(/Chrome\/([\d.]+)/);
  const version = firefox ? firefox[1] : chrome ? chrome[1] : '';
  return {
    user_agent: ua,
    browser_info: [innerWidth, innerHeight, outerWidth, outerHeight,
      screen.availWidth, screen.availHeight, screen.width, screen.height, navigator.platform].join('|'),
    params: {
      device_platform: 'webapp', aid: '6383', channel: 'channel_pc_web',
      pc_client_type: '1', version_code: '290100', version_name: '29.1.0',
      update_version_code: '170400', cookie_enabled: String(navigator.cookieEnabled),
      screen_width: String(screen.width), screen_height: String(screen.height),
      browser_language: navigator.language, browser_platform: navigator.platform,
      browser_name: firefox ? 'Firefox' : 'Chrome', browser_version: version,
      browser_online: String(navigator.onLine), engine_name: firefox ? 'Gecko' : 'Blink',
      engine_version: version, os_name: 'Windows', os_version: '10', platform: 'PC',
      cpu_core_num: String(navigator.hardwareConcurrency || 4),
      ...(navigator.deviceMemory ? {device_memory: String(navigator.deviceMemory)} : {})
    },
    token: localStorage.getItem('xmst') || ''
  };
}"""


def signed_url(url, params, cookies, profile, *, now_ms=None):
    """Return a fully encoded URL; transports must not re-encode its query."""
    uifid = next((cookies.get(key) for key in VISITOR_COOKIES if cookies.get(key)), "")
    if not uifid:
        raise PlatformBlockedError("抖音访客会话尚未建立：缺少 UIFID")
    values = {**profile["params"], **params}
    for key in SIGNATURE_FIELDS | {"msToken"}:
        values.pop(key, None)
    token = cookies.get("msToken") or profile.get("token")
    if token:
        values["msToken"] = token
    # Match the pinned upstream's percent-encoding, including Chinese handles,
    # literal '+', '/' and the '=' padding in the a_bogus alphabet.
    query = urlencode(values, quote_via=quote, safe="*-._")
    stamp_ms = int(time.time() * 1000) if now_ms is None else now_ms
    values["a_bogus"] = ABogus(
        profile["user_agent"], browser_info=profile["browser_info"]
    ).get_value(query, now_ms=stamp_ms)
    if cookies.get("s_v_web_id"):
        values["verifyFp"] = values["fp"] = cookies["s_v_web_id"]
    values["uifid"] = uifid
    values["timestamp"] = str(stamp_ms // 1000)
    query = urlencode(values, quote_via=quote, safe="*-._")
    signature = hashlib.md5(
        f"{uifid}_{values['timestamp']}_{WEB_SIGN_SALT}_{query}".encode()
    ).hexdigest()
    return url + "?" + query + "&x-secsdk-web-signature=" + signature


class DouyinProtocolTransport:
    """Author-only configured session on the discovery's existing proxy lease."""

    handles_douyin_signing = True

    def __init__(self, crawler, cookie):
        self.crawler = crawler
        self._author_identity_cache = crawler._author_identity_cache
        self._author_identity_repository = getattr(crawler, "_author_identity_repository", None)
        self.cookies = dict(
            part.strip().split("=", 1) for part in cookie.split(";") if "=" in part
        )
        # CurlAsyncHttpClient uses chrome124. Keep the UA and query in agreement
        # with that transport; browser fallback reads its own actual fingerprint.
        self.profile = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "browser_info": DEFAULT_BROWSER_INFO,
            "params": {
                "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
                "pc_client_type": "1", "version_code": "290100", "version_name": "29.1.0",
                "update_version_code": "170400", "cookie_enabled": "true",
                "screen_width": "1920", "screen_height": "1080", "browser_language": "zh-CN",
                "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "124.0.0.0",
                "browser_online": "true", "engine_name": "Blink", "engine_version": "124.0.0.0",
                "os_name": "Windows", "os_version": "10", "platform": "PC",
                "cpu_core_num": "8", "device_memory": "8",
            },
        }

    async def cookie_header(self, origin):
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    async def _get_json(self, url, *, params=None, headers=None):
        if not url.startswith("https://www.douyin.com/aweme/v1/web/"):
            raise PlatformCrawlerError("抖音会话不能用于其他来源")
        request_headers = {
            "Cookie": await self.cookie_header("https://www.douyin.com"),
            "User-Agent": self.profile["user_agent"],
            "Referer": (headers or {}).get("Referer", "https://www.douyin.com/"),
            "Accept": "application/json, text/plain, */*",
        }
        response = await self.crawler._get_response(
            signed_url(url, params or {}, self.cookies, self.profile), headers=request_headers
        )
        response_cookies = getattr(response, "cookies", None)
        if response_cookies is not None:
            self.cookies.update(dict(response_cookies.items()))
        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformBlockedError("抖音接口返回空包或非 JSON，不能视作列表结束") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("抖音接口响应结构无效")
        return payload
