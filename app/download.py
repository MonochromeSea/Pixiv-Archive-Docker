import os
import re
import time
import shutil
from urllib.parse import urlparse
from dotenv import load_dotenv
from app import paths
from app.pixiv import (
    get_pixiv_client,
    _build_session_with_referer,
    apply_image_mirror,
    PixivAuthError,
    PixivNetworkError,
    PixivError,
)

load_dotenv(paths.ENV_FILE)

REQUEST_TIMEOUT = 30
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _safe_name(name, fallback="untitled"):
    name = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    name = name.rstrip(". ") or fallback
    return name[:150]


def _sleep_interruptible(delay_ms, cancel_event=None):
    """分段限速睡眠（0.1s 粒度），期间可被取消。返回是否被取消。"""
    remain = delay_ms / 1000.0
    while remain > 1e-9:
        if cancel_event is not None and cancel_event.is_set():
            return True
        time.sleep(0.1 if remain >= 0.1 else remain)
        remain -= 0.1
    return False


def _author_display_name(client, pixiv_user_id):
    """取画师显示名（user_detail），失败回退为 ID 字符串。"""
    try:
        detail = client.api.user_detail(pixiv_user_id)
        if not detail.get("error"):
            name = (detail.get("user") or {}).get("name", "")
            if name:
                return name
    except Exception:
        pass
    return str(pixiv_user_id)


def list_new_works_since(client, pixiv_user_id, last_pid, cancel_event=None,
                         max_pages_per_type=50):
    """拉取画师比 last_pid 更新的作品（插画+漫画）。

    user_illusts 按发布时间倒序返回，逐页收集 id > last_pid 的作品，
    一旦整页都已见过（出现 id <= last_pid）即停止翻页——避免全量比对历史 ID。
    last_pid 为 None/0 时等同全量。返回 (author_name, new_illusts)。
    """
    author_name = _author_display_name(client, pixiv_user_id)
    threshold = last_pid or 0
    new_works = []
    seen = set()
    for work_type in ("illust", "manga"):
        offset = None
        for _ in range(max_pages_per_type):
            if cancel_event is not None and cancel_event.is_set():
                break
            batch, next_url = client.list_user_illusts(
                pixiv_user_id, illust_type=work_type, offset=offset
            )
            page_has_old = False
            for item in batch:
                pid = item.get("id", 0)
                if pid and pid <= threshold:
                    page_has_old = True
                    continue
                if pid and pid not in seen:
                    seen.add(pid)
                    new_works.append(item)
            if page_has_old or not next_url:
                break
            qs = client.parse_next_qs(next_url)
            offset = qs.get("offset")
            if not offset:
                break
    return author_name, new_works


def check_subscription(client, session, pixiv_user_id, last_pid, source_dir,
                       delay_ms=800, progress_callback=None, cancel_event=None):
    """检查单个订阅：只下载比 last_pid 更新的作品。

    游标推进策略：下载成功或文件已存在都视为"已见"（推进游标）；
    下载失败的 PID 不推进（下轮自动重试）。auth 错误向上抛由编排层中断。
    """
    author_name, new_works = list_new_works_since(
        client, pixiv_user_id, last_pid, cancel_event=cancel_event
    )
    new_works.sort(key=lambda x: x.get("id", 0))  # 旧→新下载，游标推进更稳

    result = {
        "pixiv_user_id": pixiv_user_id,
        "author_name": author_name,
        "new_found": len(new_works),
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "max_pid_seen": last_pid,
        "retry_pids": [],
        "details": [],
    }

    author_root = os.path.join(source_dir, _safe_name(author_name))
    for idx, illust in enumerate(new_works):
        if cancel_event is not None and cancel_event.is_set():
            break
        pixiv_id = illust.get("id", 0)
        if progress_callback:
            progress_callback(
                "download", idx + 1, len(new_works),
                f"下载 {author_name} 新作品 {idx + 1}/{len(new_works)}（PID {pixiv_id}）",
            )
        if delay_ms > 0:
            if _sleep_interruptible(delay_ms, cancel_event):
                break

        status, err = download_single_illust(session, illust, author_root, author_name)
        if status in ("downloaded", "skipped"):
            result[status] += 1
            if pixiv_id > (result["max_pid_seen"] or 0):
                result["max_pid_seen"] = pixiv_id
        else:
            result["failed"] += 1
            result["retry_pids"].append(pixiv_id)
            result["details"].append(
                {"pixiv_id": pixiv_id, "status": "failed", "error": err}
            )

    return result


def _collect_author_candidates(client, source, my_user_id, delay_ms=800,
                               cancel_event=None, max_pages=20):
    """从关注列表或收藏作品中收集候选画师。返回 [(user_id, name)]（去重）。"""
    candidates = []
    seen_uids = set()

    def add(user_obj):
        uid = (user_obj or {}).get("id")
        if uid and uid not in seen_uids:
            seen_uids.add(uid)
            candidates.append((uid, user_obj.get("name", "") or str(uid)))

    if source in ("following", "both"):
        offset = None
        for _ in range(max_pages):
            if cancel_event is not None and cancel_event.is_set():
                return candidates
            users, next_url = client.list_user_following(my_user_id, offset=offset)
            for u in users:
                add(u)
            if not next_url:
                break
            offset = client.parse_next_qs(next_url).get("offset")
            if not offset:
                break
            if delay_ms > 0 and _sleep_interruptible(delay_ms, cancel_event):
                return candidates

    if source in ("bookmarks", "both"):
        max_bookmark_id = None
        for _ in range(max_pages):
            if cancel_event is not None and cancel_event.is_set():
                return candidates
            illusts, next_url = client.list_user_bookmarks(
                my_user_id, max_bookmark_id=max_bookmark_id
            )
            for illust in illusts:
                add(illust.get("user"))
            if not next_url:
                break
            max_bookmark_id = client.parse_next_qs(next_url).get("max_bookmark_id")
            if not max_bookmark_id:
                break
            if delay_ms > 0 and _sleep_interruptible(delay_ms, cancel_event):
                return candidates

    return candidates


def import_subscriptions(client, source, my_user_id, delay_ms=800,
                         progress_callback=None, cancel_event=None):
    """把关注/收藏的画师批量导入为订阅（已存在的跳过）。直接写库。"""
    from app.database import get_db

    candidates = _collect_author_candidates(
        client, source, my_user_id, delay_ms=delay_ms, cancel_event=cancel_event
    )
    added = 0
    skipped_existing = 0
    with get_db() as conn:
        for uid, name in candidates:
            if cancel_event is not None and cancel_event.is_set():
                break
            cur = conn.execute(
                "INSERT OR IGNORE INTO subscriptions (pixiv_user_id, name) VALUES (?, ?)",
                (uid, name),
            )
            if cur.rowcount:
                added += 1
            else:
                skipped_existing += 1
                conn.execute(
                    "UPDATE subscriptions SET name = COALESCE(NULLIF(name, ''), ?) "
                    "WHERE pixiv_user_id = ?",
                    (name, uid),
                )
            if progress_callback:
                progress_callback(
                    "import", added + skipped_existing, len(candidates),
                    f"导入订阅 {added + skipped_existing}/{len(candidates)}",
                )
    return {"source": source, "found": len(candidates), "added": added,
            "skipped_existing": skipped_existing}
    """计算单作品目标目录与文件名前缀（单页/多页规则，与扫描入库命名一致）。"""
    dest_dir = author_root
    prefix = f"[{_safe_name(author_name)}] {pixiv_id}"
    if page_count > 1:
        dest_dir = os.path.join(
            author_root, f"[{_safe_name(author_name)}] {_safe_name(title)}"
        )
        prefix = f"[{_safe_name(title)}] {pixiv_id}_p"
    return dest_dir, prefix


def _resolve_dest(author_root, author_name, pixiv_id, title, page_count):
    """计算单作品目标目录与文件名前缀（单页/多页规则，与扫描入库命名一致）。"""
    dest_dir = author_root
    prefix = f"[{_safe_name(author_name)}] {pixiv_id}"
    if page_count > 1:
        dest_dir = os.path.join(
            author_root, f"[{_safe_name(author_name)}] {_safe_name(title)}"
        )
        prefix = f"[{_safe_name(title)}] {pixiv_id}_p"
    return dest_dir, prefix


def download_single_illust(session, illust, author_root, author_name):
    """下载单个 illust 的全部页（.part 原子写）。

    返回 (status, err_msg)：status 为 'downloaded' / 'skipped'；
    认证类错误以 PixivAuthError 抛出（供上层中断整批）；其它失败以 ('failed', msg) 返回。
    """
    pixiv_id = illust.get("id", 0)
    title = illust.get("title", "")
    page_count = max(1, illust.get("page_count", 1))
    urls = _extract_original_urls(illust)
    dest_dir, _prefix = _resolve_dest(author_root, author_name, pixiv_id, title, page_count)

    if _work_exists(dest_dir, _prefix):
        return "skipped", ""

    try:
        for p, url in enumerate(urls):
            if not url:
                continue
            ext = _url_ext(url) or ".jpg"
            if page_count > 1:
                filename = f"[{_safe_name(title)}] {pixiv_id}_p{p}{ext}"
            else:
                filename = f"[{_safe_name(author_name)}] {pixiv_id}{ext}"
            dest = os.path.join(dest_dir, filename)
            if os.path.exists(dest):
                continue
            os.makedirs(dest_dir, exist_ok=True)
            # 先写 .part 再原子改名：中断/超时不会留下半截文件，
            # 避免扫描时把残损 JPEG 当成完整作品入库
            tmp = dest + ".part"
            try:
                with session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as resp:
                    if resp.status_code != 200:
                        raise PixivNetworkError(f"HTTP {resp.status_code}")
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(resp.raw, f)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        return "downloaded", ""
    except PixivAuthError:
        raise
    except PixivError as e:
        return "failed", str(e)
    except Exception as e:
        return "failed", str(e)


def _list_author_works(client, author_id):
    """获取作者全部作品（插画+漫画）。返回 (author_name, illusts)。"""
    try:
        detail = client.api.user_detail(author_id)
    except Exception as e:
        raise PixivNetworkError(f"获取作者信息失败：{e}")
    if detail.get("error"):
        raise PixivError(str(detail.get("error")))
    user = detail.get("user") or {}
    author_name = user.get("name", "") or str(author_id)

    illusts = []
    seen = set()
    for work_type in ("illust", "manga"):
        offset = None
        while True:
            try:
                response = client.api.user_illusts(author_id, type=work_type, offset=offset)
            except Exception as e:
                raise PixivNetworkError(f"获取作品列表失败：{e}")
            if response.get("error"):
                raise PixivError(str(response.get("error")))
            batch = response.get("illusts") or []
            if not batch:
                break
            for item in batch:
                pid = item.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    illusts.append(item)
            next_url = response.get("next_url")
            if not next_url:
                break
            try:
                from pixivpy3 import AppPixivAPI
                qs = AppPixivAPI.parse_qs(next_url)
                offset = qs.get("offset")
            except Exception:
                offset = None
            if not offset:
                break
    return author_name, illusts


def _extract_original_urls(illust):
    pages = illust.get("meta_pages") or []
    if pages:
        return [
            apply_image_mirror(p.get("image_urls", {}).get("original", ""))
            for p in pages
            if p.get("image_urls", {}).get("original")
        ]
    single = illust.get("meta_single_page", {}).get("original_image_url", "")
    if single:
        return [apply_image_mirror(single)]
    return []


def _url_ext(url):
    path = urlparse(url).path
    return os.path.splitext(path)[1].lower()


def _work_exists(dest_dir, prefix):
    """目录中已有文件名以 prefix 开头（任意扩展名）即视为已存在。"""
    if not os.path.isdir(dest_dir):
        return False
    try:
        for name in os.listdir(dest_dir):
            if name.startswith(prefix):
                return True
    except Exception:
        return False
    return False


def download_author_works(author_id, progress_callback=None, cancel_event=None):
    source_dir = os.getenv("IMAGE_SOURCE_DIR", "").strip()
    if not source_dir:
        raise PixivError("未设置本地图片目录")
    if not os.path.isdir(source_dir):
        raise PixivError(f"图片目录不存在：{source_dir}")

    delay_str = (os.getenv("SYNC_DELAY_MS", "") or "").strip()
    delay_ms = int(delay_str) if delay_str.isdigit() else 800
    delay_ms = max(0, min(delay_ms, 10000))

    client = get_pixiv_client()
    client._ensure_auth()

    if progress_callback:
        progress_callback("list", 0, None, "正在获取作者信息…")
    author_name, illusts = _list_author_works(client, author_id)
    total = len(illusts)
    if progress_callback:
        progress_callback("list", 1, total, f"共获取 {total} 件作品")

    results = {
        "author_id": author_id,
        "author_name": author_name,
        "total": total,
        "downloaded": 0,
        "skipped": 0,
        "failed": 0,
        "cancelled": False,
        "auth_error": None,
        "details": [],
    }

    # 图片字节下载跟随连接模式：proxy 模式（含 auto 回退）时同样走代理，
    # 否则环境被封时 API 能通而图片下载必然失败
    session = _build_session_with_referer(
        "https://www.pixiv.net/", use_proxy=(client.mode == "proxy")
    )
    author_root = os.path.join(source_dir, _safe_name(author_name))

    for idx, illust in enumerate(illusts):
        if cancel_event is not None and cancel_event.is_set():
            results["cancelled"] = True
            break

        pixiv_id = illust.get("id", 0)

        if progress_callback:
            progress_callback(
                "download", idx + 1, total,
                f"下载 {author_name} 作品 {idx + 1}/{total}（PID {pixiv_id}）",
            )

        if delay_ms > 0:
            remain = delay_ms / 1000.0
            while remain > 1e-9:
                if cancel_event is not None and cancel_event.is_set():
                    results["cancelled"] = True
                    break
                time.sleep(0.1 if remain >= 0.1 else remain)
                remain -= 0.1
            if results["cancelled"]:
                break

        # ---- skip 判定 + 下载（与订阅检查共用同一套命名/去重/原子写规则）----
        try:
            status, err = download_single_illust(session, illust, author_root, author_name)
        except PixivAuthError as e:
            results["auth_error"] = str(e)
            results["failed"] += 1
            results["details"].append(
                {"pixiv_id": pixiv_id, "status": "failed", "error": str(e)}
            )
            break
        if status == "skipped":
            results["skipped"] += 1
            results["details"].append({"pixiv_id": pixiv_id, "status": "skipped"})
        elif status == "downloaded":
            results["downloaded"] += 1
            results["details"].append({"pixiv_id": pixiv_id, "status": "downloaded"})
        else:
            results["failed"] += 1
            results["details"].append(
                {"pixiv_id": pixiv_id, "status": "failed", "error": err}
            )

    return results