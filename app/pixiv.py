import os
import time
import requests
from pixivpy3 import AppPixivAPI
from dotenv import load_dotenv
from app import paths
from app.direct_connect import enable_direct, DirectAdapter, refresh_ips

load_dotenv(paths.ENV_FILE)

REQUEST_TIMEOUT = 30


class PixivError(Exception):
    """Pixiv 操作异常基类。"""


class PixivAuthError(PixivError):
    """认证失败：refresh token 缺失 / 无效 / 认证请求失败。"""


class PixivNetworkError(PixivError):
    """网络 / 传输层失败（连接、超时、SSL 等）。"""


class PixivDeletedError(PixivError):
    """作品在 Pixiv 上已删除或不存在。"""


_AUTH_MSG_HINTS = (
    "auth", "token", "session", "login", "refresh",
    "invalid_grant", "401", "expired", "有効期限", "セッション",
)


def _looks_like_auth_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in _AUTH_MSG_HINTS)


def _fmt_error(err):
    if isinstance(err, dict):
        parts = [
            v for v in (err.get("message"), err.get("user_message"), err.get("reason"))
            if v
        ]
        return " / ".join(parts) or str(err)
    return str(err)


def _build_session():
    """Build a plain requests.Session (replaces cloudscraper for direct mode)."""
    session = requests.Session()
    adapter = DirectAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.verify = False
    session.headers.update({
        "referer": "https://app-api.pixiv.net/",
        "User-Agent": "PixivIOSApp/5.8.0",
    })
    return session


class PixivClient:
    def __init__(self):
        self.mode = os.getenv("PIXIV_MODE", "auto").strip().lower()
        self.proxy = os.getenv("PIXIV_PROXY", "")
        self._last_auth = 0
        self._auth_ttl = 3000
        self._build_api()

    def _build_api(self):
        if self.mode == "direct":
            enable_direct()
            self.api = AppPixivAPI()
            self.api.requests = _build_session()
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
        elif self.mode == "proxy" and self.proxy:
            self.api = AppPixivAPI(proxies={"http": self.proxy, "https": self.proxy})
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
        else:
            # auto: try direct first
            enable_direct()
            self.api = AppPixivAPI()
            self.api.requests = _build_session()
            self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
            self._auto_direct = True

    def _ensure_auth(self):
        refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "")
        if not refresh_token:
            raise PixivAuthError("未设置 PIXIV_REFRESH_TOKEN（请到 设置 → Pixiv Refresh Token 填写）")
        now = time.time()
        if now - self._last_auth < self._auth_ttl:
            return
        try:
            self.api.auth(refresh_token=refresh_token)
            self._last_auth = now
        except Exception:
            if getattr(self, "_auto_direct", False) and self.proxy:
                # auto mode: fall back to Clash proxy
                try:
                    self.mode = "proxy"
                    self._auto_direct = False
                    self.api = AppPixivAPI(
                        proxies={"http": self.proxy, "https": self.proxy}
                    )
                    self.api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
                    self.api.auth(refresh_token=refresh_token)
                    self._last_auth = now
                except Exception as e2:
                    raise PixivAuthError(f"认证失败（直连与代理均失败）：{e2}")
            else:
                raise PixivAuthError("认证失败，请检查 refresh token 与网络连接")

    def get_illust_detail(self, illust_id):
        try:
            self._ensure_auth()
            return self._detail_once(illust_id)
        except PixivNetworkError:
            if getattr(self, "_auto_direct", False) and self.mode == "direct":
                return self._retry_direct(illust_id)
            raise
        except PixivError:
            raise
        except Exception as e:
            raise PixivNetworkError(f"请求作品详情失败：{e}")

    def _retry_direct(self, illust_id):
        """auto-direct 模式下刷新直连 IP 并重试一次。"""
        try:
            refresh_ips()
            self._last_auth = 0
            self._ensure_auth()
            return self._detail_once(illust_id)
        except PixivError:
            raise
        except Exception as e:
            raise PixivNetworkError(f"刷新直连 IP 后仍失败：{e}")

    def _detail_once(self, illust_id):
        try:
            response = self.api.illust_detail(illust_id)
        except Exception as e:
            raise PixivNetworkError(f"请求作品详情失败：{e}")

        if response.get("error"):
            err = response.get("error")
            msg = _fmt_error(err)
            if _looks_like_auth_error(msg):
                # 疑似 token 过期等认证问题：重新认证一次再试
                try:
                    self._last_auth = 0
                    self.api.auth(refresh_token=os.getenv("PIXIV_REFRESH_TOKEN", ""))
                    response = self.api.illust_detail(illust_id)
                except Exception as e:
                    raise PixivAuthError(f"认证失效且重新认证失败：{e}")
                if response.get("error"):
                    raise PixivAuthError(f"重新认证后仍返回错误：{_fmt_error(response.get('error'))}")
            else:
                raise PixivDeletedError(msg)

        illust = response.get("illust")
        if not illust:
            raise PixivDeletedError("Pixiv 返回的作品数据为空")
        return self._parse_illust(illust)

    def _parse_illust(self, illust):
        tags = []
        for tag_data in illust.get("tags", []):
            tags.append({
                "name": tag_data.get("name", ""),
                "translated_name": tag_data.get("translated_name", ""),
            })

        meta_pages = illust.get("meta_pages", [])
        if meta_pages:
            image_urls = [mp.get("image_urls", {}).get("original", "") for mp in meta_pages]
        else:
            image_urls = [illust.get("meta_single_page", {}).get("original_image_url", "")]

        return {
            "pixiv_id": illust.get("id", 0),
            "title": illust.get("title", ""),
            "description": illust.get("caption", ""),
            "author_id": illust.get("user", {}).get("id", 0),
            "author_name": illust.get("user", {}).get("name", ""),
            "author_profile_image": illust.get("user", {}).get("profile_image_urls", {}).get("medium", ""),
            "create_date": illust.get("create_date", ""),
            "page_count": illust.get("page_count", 1),
            "width": illust.get("width", 0),
            "height": illust.get("height", 0),
            "tags": tags,
            "image_urls": image_urls,
            "total_view": illust.get("total_view", 0),
            "total_bookmarks": illust.get("total_bookmarks", 0),
        }


_pixiv_client = None


def get_pixiv_client():
    global _pixiv_client
    if _pixiv_client is None:
        _pixiv_client = PixivClient()
    return _pixiv_client


def reset_pixiv_client():
    global _pixiv_client
    _pixiv_client = None


def refresh_direct_ips():
    return refresh_ips()
