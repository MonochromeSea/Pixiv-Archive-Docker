import os
import json
import ssl
import socket
import threading
import urllib3
from requests.adapters import HTTPAdapter
from app import paths

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Hardcoded IP overrides (mirrors pixez Hoster._constMap)
DEFAULT_IP_MAP = {
    "app-api.pixiv.net": "210.140.139.155",
    "oauth.secure.pixiv.net": "210.140.139.155",
    "i.pximg.net": "210.140.139.133",
    "s.pximg.net": "210.140.139.133",
}

# DoH servers tried in order (host -> anycast IPs). Cloudflare is primary.
DOH_SERVERS = [
    ("cloudflare-dns.com", ["104.16.248.249", "104.16.249.249"]),
    ("doh.dns.sb", ["185.222.222.222", "45.11.45.11"]),
]

_patch_lock = threading.Lock()
_getaddrinfo_original = socket.getaddrinfo
_patched = False

ENV_KEYS = {
    "app-api.pixiv.net": "PIXIV_IP_APP_API",
    "oauth.secure.pixiv.net": "PIXIV_IP_OAUTH",
    "i.pximg.net": "PIXIV_IP_IMAGE",
    "s.pximg.net": "PIXIV_IP_STATIC",
}

# A reachable IP can still terminate at nginx and return 403.  Treat those
# responses as routing failures instead of saving the endpoint as healthy.
_BLOCKED_STATUS_CODES = frozenset({403, 407, 429, 502, 503, 504})
_BLOCKED_BODY_HINTS = (
    "403 forbidden",
    "cloudflare ray id",
    "cf-error-details",
)


def _env_path():
    return paths.ENV_FILE


def _read_env():
    result = {}
    path = _env_path()
    if not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
    return result


def _write_env(updates):
    path = _env_path()
    existing = _read_env()
    existing.update(updates)
    with open(path, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")


def get_ip_map():
    env = _read_env()
    mapping = {}
    for host, key in ENV_KEYS.items():
        ip = env.get(key, "").strip()
        mapping[host] = ip if ip else DEFAULT_IP_MAP[host]
    return mapping


def save_ip_override(host, ip):
    if host not in ENV_KEYS:
        return
    _write_env({ENV_KEYS[host]: ip})


def _patched_getaddrinfo(host, *args, **kwargs):
    mapping = get_ip_map()
    if host in mapping:
        host = mapping[host]
    return _getaddrinfo_original(host, *args, **kwargs)


def enable_direct():
    global _patched
    with _patch_lock:
        if _patched:
            return
        socket.getaddrinfo = _patched_getaddrinfo
        _patched = True


class DirectAdapter(HTTPAdapter):
    """HTTPAdapter that connects without TLS SNI and without cert verification."""

    def _make_ssl_context(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        orig_wrap = ctx.wrap_socket

        def wrap_without_sni(*args, **kwargs):
            kwargs["server_hostname"] = None
            return orig_wrap(*args, **kwargs)

        ctx.wrap_socket = wrap_without_sni
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._make_ssl_context()
        kwargs["server_hostname"] = None
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._make_ssl_context()
        kwargs["server_hostname"] = None
        return super().proxy_manager_for(*args, **kwargs)


def resolve_ip_via_doh(host, timeout=10):
    """Query DNS-over-HTTPS for A records of a pixiv host (best effort)."""
    for doh_host, doh_ips in DOH_SERVERS:
        for ip in doh_ips:
            try:
                ips = _doh_query(doh_host, ip, host, timeout)
                if ips:
                    return ips
            except Exception:
                continue
    return []


def _doh_query(doh_host, doh_ip, query_host, timeout):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    orig_wrap = ctx.wrap_socket

    def wrap_without_sni(*args, **kwargs):
        kwargs["server_hostname"] = None
        return orig_wrap(*args, **kwargs)

    ctx.wrap_socket = wrap_without_sni

    sock = socket.create_connection((doh_ip, 443), timeout=timeout)
    ssock = ctx.wrap_socket(sock, server_hostname=None)
    try:
        request = (
            f"GET /dns-query?name={query_host}&type=A HTTP/1.1\r\n"
            f"Host: {doh_host}\r\n"
            f"Accept: application/dns-json\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssock.sendall(request.encode())
        data = b""
        while True:
            chunk = ssock.recv(4096)
            if not chunk:
                break
            data += chunk
        body = data.split(b"\r\n\r\n", 1)[1]
        result = json.loads(body)
        answers = [
            a["data"]
            for a in result.get("Answer", [])
            if a.get("type") == 1 and a.get("data")
        ]
        return answers
    finally:
        ssock.close()


def _response_is_blocked(response):
    status = int(getattr(response, "status_code", 0) or 0)
    if status in _BLOCKED_STATUS_CODES or status >= 500:
        return True
    try:
        body = (getattr(response, "text", "") or "")[:4096].lower()
    except Exception:
        body = ""
    return any(hint in body for hint in _BLOCKED_BODY_HINTS)


def _probe_request(session, host, timeout):
    if host == "oauth.secure.pixiv.net":
        # An invalid probe token produces a JSON 400 on a healthy endpoint,
        # while a blocked route returns nginx/Cloudflare HTML.
        return session.post(
            f"https://{host}/auth/token",
            data={"grant_type": "refresh_token", "refresh_token": "__ip_probe__"},
            timeout=timeout,
        )
    return session.get(f"https://{host}/", timeout=timeout)


def _probe_ip(host, ip, timeout, use_sni):
    """Probe a candidate IP with either standard SNI or the legacy direct TLS.

    返回值三态：
      True  —— 拿到正常响应，可用；
      False —— 能连上但对端返回拦截页（403/Cloudflare 等）；
      None  —— 传输层失败（超时/TLS 中断等），该 IP 无论哪种握手都不可用。
    """
    session = None
    try:
        from requests import Session
        session = Session()
        session.trust_env = False
        if use_sni:
            # Normal requests adapter: SNI and certificate verification stay on,
            # while the patched resolver still directs the hostname to the IP.
            session.verify = True
        else:
            adapter = DirectAdapter()
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.verify = False
        session.headers.update({
            "referer": "https://app-api.pixiv.net/",
            "User-Agent": "PixivIOSApp/5.8.0",
        })

        original = socket.getaddrinfo

        def forced(hostname, *args, **kwargs):
            if hostname == host:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]
            return original(hostname, *args, **kwargs)

        socket.getaddrinfo = forced
        try:
            response = _probe_request(session, host, timeout)
            return not _response_is_blocked(response)
        finally:
            socket.getaddrinfo = original
    except Exception:
        return None
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _candidate_ips(host, timeout=10):
    candidates = []

    def add(value):
        value = (value or "").strip()
        if value and value not in candidates:
            candidates.append(value)

    for ip in resolve_ip_via_doh(host, timeout=timeout):
        add(ip)
    try:
        for info in _getaddrinfo_original(host, None, socket.AF_INET):
            add(info[4][0])
    except Exception:
        pass
    add(get_ip_map().get(host))
    add(DEFAULT_IP_MAP.get(host))
    # 探测是串行的（要临时改全局 DNS 解析，不能并发），限制候选数量避免
    # 全部失败时把认证回退拖到几分钟。
    return candidates[:4]


def _validate_ip(host, ip, timeout=10):
    """Return true only when a candidate avoids an obvious upstream block page."""
    # Standard SNI first: some Pixiv edges reject SNI-less TLS with 403.
    sni_result = _probe_ip(host, ip, timeout, use_sni=True)
    if sni_result:
        return True
    if sni_result is False:
        # 能连上、只是被拦截页拒绝：无 SNI 握手是常见原因，值得再试一次。
        return _probe_ip(host, ip, timeout, use_sni=False) is True
    # 传输层失败说明这个 IP 本身不可达，换一种握手也不会成功，直接跳过。
    return False


def refresh_ips(hosts=None, timeout=6):
    """Query DoH for each pixiv host, validate, and persist working IPs.

    hosts: 只刷新指定域名（默认全部）。认证失败时只刷新 API/OAuth 两个域名，
    避免一次重建把所有域名都探一遍、白白拉长故障恢复时间。
    """
    targets = list(hosts) if hosts else list(DEFAULT_IP_MAP)
    updated = {}
    for host in targets:
        if host not in DEFAULT_IP_MAP:
            continue
        ips = _candidate_ips(host, timeout=timeout)
        for ip in ips:
            if _validate_ip(host, ip, timeout=timeout):
                updated[ENV_KEYS[host]] = ip
                break
    if updated:
        _write_env(updated)
    return updated
