import os
import re
import logging
import time
import requests
from urllib.parse import urlsplit
from pixivpy3 import AppPixivAPI
from dotenv import load_dotenv
from app import paths
from app.direct_connect import enable_direct, DirectAdapter, refresh_ips
from app.tag_rules import normalize_ai_type

load_dotenv(paths.ENV_FILE)

log = logging.getLogger("pixiv_archive.pixiv")

REQUEST_TIMEOUT = 30

# 官方图片主机（下载/头像字节的来源），镜像替换只作用于这些域名
_PIXIV_IMAGE_HOSTS = frozenset({"i.pximg.net", "i-f.pximg.net", "s.pximg.net"})
_SCHEME_RE = re.compile(r"^https?://")


def image_mirror_host():
    """设置里配置的第三方图片镜像域名（PIXIV_IMAGE_MIRROR，留空=不启用）。"""
    mirror = (os.getenv("PIXIV_IMAGE_MIRROR", "") or "").strip()
    if not mirror:
        return ""
    mirror = _SCHEME_RE.sub("", mirror).strip().rstrip("/")
    # 只保留主机+路径前缀部分，拒绝含空格的无效输入
    if not mirror or re.search(r"\s", mirror):
        return ""
    return mirror


def apply_image_mirror(url):
    """若启用了镜像，把 pixiv 官方图片 URL 的域名替换为镜像域名（保留路径与查询串）。

    仅影响图片字节的来源，不改变 API 调用与 URL 获取逻辑；
    非官方图片主机（含已是镜像的 URL）原样返回，保证幂等。
    """
    if not url:
        return url
    mirror = image_mirror_host()
    if not mirror:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host not in _PIXIV_IMAGE_HOSTS:
        return url
    if not _SCHEME_RE.match(mirror):
        mirror = "https://" + mirror
    return mirror + parsed.path + (f"?{parsed.query}" if parsed.query else "")


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

# 传输层失败（IP 失效、握手被掐、超时）与 token 失效要分开处理：
# 前者可以刷新直连地址后重试，后者只能让用户换 token。
_TRANSPORT_HINTS = (
    "ssleo", "unexpected_eof", "eof occurred", "eof", "timed out", "timeout",
    "connection", "reset", "unreachable", "refused", "network", "handshake",
)

# These responses mean the request reached a proxy/CDN/edge but not the Pixiv
# API. They are not evidence that the refresh token is invalid.
_UPSTREAM_ROUTE_HINTS = (
    "403", "forbidden", "nginx", "bad gateway", "gateway timeout",
    "502", "503", "504", "cloudflare",
)


def _looks_like_transport_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in _TRANSPORT_HINTS)


def _looks_like_upstream_route_error(msg):
    m = (msg or "").lower()
    return any(k in m for k in _UPSTREAM_ROUTE_HINTS)


def _looks_like_direct_retry_error(msg):
    return _looks_like_transport_error(msg) or _looks_like_upstream_route_error(msg)


def _looks_like_invalid_token_error(msg):
    m = (msg or "").lower()
    return (
        "invalid_grant" in m
        or "invalid refresh token" in m
        or "refresh token expired" in m
        or "token has expired" in m
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


def _configured_proxy():
    """返回 Pixiv 请求可用的代理地址。

    优先使用显式的 PIXIV_PROXY；未设置时回退到容器常见的 HTTPS_PROXY /
    HTTP_PROXY，保证「Clash 以环境变量注入」的部署方式仍然可用。
    direct 模式不会调用本函数，因此直连不会被环境变量悄悄改道。
    """
    for key in ("PIXIV_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.getenv(key, "") or "").strip()
        if value:
            return value
    return ""


def _build_session(direct_sni=False):
    """Build a plain requests.Session (replaces cloudscraper for direct mode)."""
    return _build_session_with_referer(
        "https://app-api.pixiv.net/", direct_sni=direct_sni
    )


def _build_session_with_referer(referer, use_proxy=False, direct_sni=False):
    session = requests.Session()
    # 只走这里显式选择的线路：direct 模式的请求不再被容器里的
    # HTTP_PROXY/HTTPS_PROXY 悄悄改道；代理地址由 _configured_proxy() 统一解析。
    session.trust_env = False
    proxy = _configured_proxy() if use_proxy else ""
    if proxy:
        # 代理模式走标准 TLS（SNI + 完整证书校验）：DirectAdapter 的无 SNI 握手
        # 是给 IP 直连用的，经代理隧道时 Cloudflare 对无 SNI 连接会直接掐断
        # （SSLEOFError）。requests.Session 原生支持 proxies，无需挂自定义 adapter。
        session.proxies.update({"http": proxy, "https": proxy})
    elif direct_sni:
        # Standard TLS keeps SNI and certificate verification enabled while
        # socket.getaddrinfo still maps Pixiv hostnames to the selected IP.
        session.verify = True
    else:
        adapter = DirectAdapter()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.verify = False
    session.headers.update({
        "referer": referer,
        "User-Agent": "PixivIOSApp/5.8.0",
    })
    return session


def fetch_profile_image(url, timeout=REQUEST_TIMEOUT):
    """Download a pixiv author profile image (for avatar cache). Returns bytes or None."""
    if not url:
        return None
    try:
        enable_direct()
        try:
            use_proxy = get_pixiv_client().mode == "proxy"
        except Exception:
            use_proxy = False
        session = _build_session_with_referer("https://www.pixiv.net/", use_proxy=use_proxy)
        candidates = [url]
        mirrored = apply_image_mirror(url)
        if mirrored and mirrored != url:
            # 优先走镜像，失败回退官方地址（部分镜像不服务 s.pximg.net 头像）
            candidates.insert(0, mirrored)
        for candidate in candidates:
            try:
                resp = session.get(candidate, timeout=timeout)
                if resp.status_code == 200:
                    return resp.content
            except Exception:
                continue
        return None
    except Exception:
        return None


class PixivClient:
    def __init__(self):
        self.mode = os.getenv("PIXIV_MODE", "auto").strip().lower()
        self.proxy = _configured_proxy()
        if self.mode == "proxy" and not self.proxy:
            # 配置了 proxy 模式却没有可用代理时，退回 auto：
            # 否则直连的 403/超时既不会走代理回退，也不会走直连自修复。
            log.warning(
                "PIXIV_MODE=proxy but no PIXIV_PROXY/HTTPS_PROXY is configured; "
                "using automatic direct mode instead"
            )
            self.mode = "auto"
        self._last_auth = 0
        self._auth_ttl = 3000
        self._auto_direct = False
        self._build_api()

    def _new_api(self, use_proxy=False, direct_sni=False):
        if not use_proxy:
            enable_direct()
        api = AppPixivAPI()
        api.requests = _build_session_with_referer(
            "https://app-api.pixiv.net/",
            use_proxy=use_proxy,
            direct_sni=direct_sni,
        )
        api.requests_kwargs = {"timeout": REQUEST_TIMEOUT}
        return api

    def _build_api(self):
        self._auto_direct = self.mode == "auto"
        if self.mode == "proxy" and self.proxy:
            # 注意：不能用 AppPixivAPI(proxies=...) 再赋值 requests_kwargs——
            # 构造函数把 proxies 存进 requests_kwargs，后续整体赋值会把它覆盖丢失
            self.api = self._new_api(use_proxy=True)
        else:
            # direct、以及未配置代理的 auto/proxy，都先走直连。
            self.api = self._new_api(direct_sni=False)

    def _ensure_auth(self):
        refresh_token = os.getenv("PIXIV_REFRESH_TOKEN", "")
        if not refresh_token:
            raise PixivAuthError("未设置 PIXIV_REFRESH_TOKEN（请到 设置 → Pixiv Refresh Token 填写）")
        now = time.time()
        if now - self._last_auth < self._auth_ttl:
            return
        causes = []
        configured_mode = self.mode

        # 1) 当前模式直接认证
        try:
            self.api.auth(refresh_token=refresh_token)
            self._last_auth = now
            return
        except Exception as e:
            causes.append(str(e))
            log.warning("pixiv auth failed in %s mode: %s", configured_mode, e)

        # 2) auto 模式且配置了代理：回退到代理再试
        if configured_mode == "auto" and self.proxy:
            try:
                candidate_api = self._new_api(use_proxy=True)
                candidate_api.auth(refresh_token=refresh_token)
                # Commit the mode only after authentication succeeds, so a
                # failed proxy fallback cannot leave the client in proxy mode.
                self.api = candidate_api
                self.mode = "proxy"
                self._auto_direct = False
                self._last_auth = now
                log.info("pixiv auth recovered via proxy fallback")
                return
            except Exception as e:
                causes.append(str(e))
                log.warning("pixiv auth proxy fallback failed: %s", e)

        # 3) 直连/auto 模式：403、IP 失效或握手被掐时，先尝试标准 SNI，
        #    再刷新直连地址。部分 Pixiv 边缘节点会拒绝无 SNI 的 TLS 请求。
        if configured_mode in ("direct", "auto") and _looks_like_direct_retry_error(causes[0]):
            try:
                log.info("pixiv auth trying standard SNI direct fallback")
                candidate_api = self._new_api(direct_sni=True)
                candidate_api.auth(refresh_token=refresh_token)
                self.api = candidate_api
                self.mode = configured_mode
                self._auto_direct = configured_mode == "auto"
                self._last_auth = now
                log.info("pixiv auth recovered with standard SNI direct connection")
                return
            except Exception as e:
                causes.append(str(e))
                log.info("pixiv auth standard SNI direct fallback failed: %s", e)

            try:
                refreshed = refresh_ips(
                    hosts=("app-api.pixiv.net", "oauth.secure.pixiv.net")
                )
                if refreshed:
                    log.info("pixiv direct IPs refreshed: %s", refreshed)
                    for direct_sni, label in ((True, "SNI"), (False, "legacy")):
                        try:
                            candidate_api = self._new_api(direct_sni=direct_sni)
                            candidate_api.auth(refresh_token=refresh_token)
                            self.api = candidate_api
                            self.mode = configured_mode
                            self._auto_direct = configured_mode == "auto"
                            self._last_auth = now
                            log.info(
                                "pixiv auth recovered after direct IP refresh (%s)",
                                label,
                            )
                            return
                        except Exception as e:
                            causes.append(str(e))
                            log.info(
                                "pixiv auth after IP refresh failed (%s): %s",
                                label,
                                e,
                            )
                else:
                    log.info("pixiv direct IP refresh found no usable endpoint")
                    causes.append("DoH 未找到可通过校验的直连 IP")
            except Exception as e:
                causes.append(str(e))
                log.warning("pixiv direct IP refresh failed: %s", e)

        # 先截断原始异常，再追加中文提示，避免提示被 [:300] 裁掉。
        cause = " | ".join(c for c in causes if c)[:220]
        if _looks_like_invalid_token_error(cause):
            cause += "（refresh token 已失效，请到设置页重新获取并填写）"
        elif _looks_like_upstream_route_error(cause):
            cause += (
                "（认证请求被 Pixiv 上游或代理以 403/nginx 拒绝，这通常不是 "
                "refresh token 无效；请检查直连节点/代理分流，或刷新直连 IP）"
            )
        elif _looks_like_transport_error(cause):
            cause += (
                "（常见原因：容器无法直连 Pixiv，或代理未把 oauth.secure.pixiv.net "
                "走节点，请检查代理分流规则）"
            )
        # 失败后清掉认证缓存，下一次同步请求会重新尝试，而不是在 TTL 内继续沿用。
        self._last_auth = 0
        raise PixivAuthError(f"认证失败：{cause}")

    def ensure_auth(self):
        """公开的认证入口，供业务代码调用而不必碰私有方法。"""
        self._ensure_auth()

    def get_illust_detail(self, illust_id):
        try:
            self._ensure_auth()
            return self._detail_once(illust_id)
        except PixivNetworkError:
            if self.mode in ("direct", "auto"):
                return self._retry_direct(illust_id)
            raise
        except PixivError:
            raise
        except Exception as e:
            raise PixivNetworkError(f"请求作品详情失败：{e}")

    @staticmethod
    def parse_next_qs(next_url):
        """把 pixiv 返回的 next_url 解析成翻页参数 dict（offset / max_bookmark_id 等）。"""
        return AppPixivAPI.parse_qs(next_url)

    def list_user_bookmarks(self, user_id, max_bookmark_id=None):
        """取用户公开收藏列表一页（按收藏时间倒序）。返回 (illusts, next_url)。"""
        try:
            self._ensure_auth()
            response = self.api.user_bookmarks_illust(
                user_id, max_bookmark_id=max_bookmark_id
            )
        except Exception as e:
            raise PixivNetworkError(f"请求收藏列表失败：{e}")
        if response.get("error"):
            raise PixivNetworkError(_fmt_error(response.get("error")))
        return response.get("illusts", []), response.get("next_url")

    def get_user_display_name(self, user_id):
        """取用户显示名（user_detail），失败回退为 ID 字符串。"""
        try:
            self._ensure_auth()
            detail = self.api.user_detail(user_id)
            if not detail.get("error"):
                name = (detail.get("user") or {}).get("name", "")
                if name:
                    return name
        except Exception:
            pass
        return str(user_id)

    def _retry_direct(self, illust_id):
        """auto-direct 模式下刷新直连 IP 并重试一次。"""
        try:
            refresh_ips(hosts=("app-api.pixiv.net",))
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

        ai_type, ai_type_source = _extract_ai_type(illust)
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
            "ai_type": ai_type,
            "ai_type_source": ai_type_source,
            "tags": tags,
            "image_urls": image_urls,
            "total_view": illust.get("total_view", 0),
            "total_bookmarks": illust.get("total_bookmarks", 0),
        }


def _extract_ai_type(illust):
    """Read Pixiv AI status across App API/library field spellings."""
    def read_value(key):
        if isinstance(illust, dict):
            return illust.get(key)
        getter = getattr(illust, "get", None)
        if callable(getter):
            try:
                value = getter(key)
                if value is not None:
                    return value
            except Exception:
                pass
        return getattr(illust, key, None)

    for key in ("illust_ai_type", "aiType", "ai_type", "ai-type"):
        raw = read_value(key)
        if raw is None:
            continue
        value = normalize_ai_type(raw)
        if value is not None:
            return value, key
    return None, ""


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
