import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from queue import Queue, Full, Empty
from urllib.parse import unquote, parse_qs
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import paths
from app import jobs
from app.database import get_db, init_db
from app.scanner import scan_directory, _normalize_image_pages, refresh_duplicate_candidates
from app.thumbnails import generate_thumbnail, generate_image_thumbnail
from app.sync import sync_metadata
from app.download import check_bookmark_subscription
from app.organizer import PATH_RULES, RENAME_RULES, organize_files, undo_last_organize
from app.tag_rules import R18_TAG_NAMES
from app.pixiv import reset_pixiv_client, fetch_profile_image, get_pixiv_client
from app.watcher import FolderWatcher
from app.events import publish, stream as event_stream

load_dotenv(paths.ENV_FILE)

logging.basicConfig(
    level=getattr(logging, (os.getenv("PA_LOG_LEVEL", "INFO") or "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pixiv_archive")

# ---- 局域网访问控制 ----
# PA_HOST 由 run.py / launcher.py 在导入本模块前写入环境。
DEFAULT_PORT = 6814
_SYNC_DELAY_DEFAULT = 800
_SYNC_DELAY_MAX = 10000
_FAIL_PAUSE_DEFAULT = 10
_FAIL_PAUSE_MAX = 100


def _parse_sync_delay(raw):
    s = (raw or "").strip()
    if not s:
        return _SYNC_DELAY_DEFAULT
    if s.isdigit():
        v = int(s)
        return v if v <= _SYNC_DELAY_MAX else _SYNC_DELAY_MAX
    return _SYNC_DELAY_DEFAULT


def _bool_env(raw, default=False):
    s = (raw or "").strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "on")


def _parse_interval_minutes(raw, default=60):
    s = (raw or "").strip()
    if s.isdigit():
        return max(5, min(int(s), 1440))
    return default


def _parse_sse_release_delay(raw, default=60):
    s = (raw or "").strip()
    if s.isdigit():
        return max(10, min(int(s), 86400))
    return default


def _parse_weekday(raw, default=0):
    s = (raw or "").strip()
    if s.isdigit():
        return max(0, min(int(s), 6))
    return default


def _parse_hhmm(raw, default="03:30"):
    s = (raw or "").strip()
    if re.match(r"^\d{1,2}:\d{2}$", s):
        hh, mm = [int(x) for x in s.split(":", 1)]
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    return default


def _parse_fail_pause_threshold(raw, default=_FAIL_PAUSE_DEFAULT):
    s = (raw or "").strip()
    if s.isdigit():
        return max(2, min(int(s), _FAIL_PAUSE_MAX))
    return default


def _format_local_time_from_path(path):
    try:
        st = os.stat(path)
    except Exception:
        return {"created": "未知", "modified": "未知"}
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "未知"
    created = getattr(st, "st_birthtime", None)
    if created is None:
        created = getattr(st, "st_ctime", None)
    return {"created": fmt(created), "modified": fmt(getattr(st, "st_mtime", None))}
HOST = os.getenv("PA_HOST", "127.0.0.1").strip() or "127.0.0.1"
LAN_MODE = HOST not in ("127.0.0.1", "localhost", "::1")
ACCESS_TOKEN = os.getenv("PA_ACCESS_TOKEN", "").strip()
if LAN_MODE and not ACCESS_TOKEN:
    ACCESS_TOKEN = secrets.token_urlsafe(12)

_PUBLIC_PREFIXES = ("/static/", "/thumbnails/")
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
_SETTING_ENV_KEYS = (
    "PIXIV_REFRESH_TOKEN",
    "IMAGE_SOURCE_DIR",
    "IMAGE_SOURCE_DIRS",
    "PIXIV_PROXY",
    "PIXIV_MODE",
    "PIXIV_IMAGE_MIRROR",
    "PA_PORT",
    "PA_ACCESS_TOKEN",
    "SYNC_DELAY_MS",
    "AUTO_WATCH_ENABLED",
    "PA_SSE_RELEASE_DELAY_SECONDS",
    "PA_SCAN_HASH_MODE",
    "PA_SHOW_FOLDER_BTN",
    "PA_VIEWER_AUTO_ORIGINAL",
    "PA_SIDEBAR_HOVER_EXPAND",
    "BOOKMARK_AUTO_CHECK_ENABLED",
    "BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES",
    "BOOKMARK_AUTO_CHECK_WEEKDAY",
    "BOOKMARK_AUTO_CHECK_TIME",
    "AUTO_FAIL_PAUSE_ENABLED",
    "AUTO_FAIL_PAUSE_THRESHOLD",
    "ORGANIZE_SOURCE_DIRS",
    "ORGANIZE_OUTPUT_DIR",
    "ORGANIZE_MODE",
    "ORGANIZE_PATH_RULE",
    "ORGANIZE_PATH_TEMPLATE",
    "ORGANIZE_UNKNOWN_AS_HUMAN",
    "ORGANIZE_RENAME_ENABLED",
    "ORGANIZE_RENAME_RULE",
    "ORGANIZE_RENAME_TEMPLATE",
)


def is_local_client(host):
    return host in _LOCAL_CLIENTS


def get_lan_ip():
    """尽力获取本机局域网 IP（用于打印访问地址）。"""
    if not LAN_MODE:
        return "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return "127.0.0.1"


def lan_access_url():
    token_part = f"?token={ACCESS_TOKEN}" if LAN_MODE else ""
    return f"http://{get_lan_ip()}:{os.getenv('PA_PORT') or DEFAULT_PORT}/{token_part}"


class LANGuardMiddleware:
    """LAN 模式下，非本机请求必须携带访问令牌（?token= 或 X-Access-Token）。

    静态资源与缩略图放行（避免静态资源请求无法带 header），
    页面 / API / 原图接口一律校验。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not LAN_MODE:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(_PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        client = (scope.get("client") or ("", 0))[0]
        if is_local_client(client):
            await self.app(scope, receive, send)
            return

        ok = False
        try:
            qs = parse_qs(scope.get("query_string", b"").decode("utf-8", "ignore"))
            ok = qs.get("token", [""])[0] == ACCESS_TOKEN
        except Exception:
            ok = False
        if not ok:
            for k, v in scope.get("headers", []):
                if k.lower() == b"x-access-token" and v.decode("utf-8", "ignore") == ACCESS_TOKEN:
                    ok = True
                    break

        if ok:
            await self.app(scope, receive, send)
            return

        accept = b""
        for k, v in scope.get("headers", []):
            if k.lower() == b"accept":
                accept = v
                break
        if b"text/html" in accept:
            body = (
                "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>需要访问令牌</title></head><body style='font-family:sans-serif;"
                "background:#101218;color:#e9eaf0;padding:40px;'>"
                "<h2>需要访问令牌</h2>"
                "<p>此服务已开启局域网访问保护。请在浏览器地址栏末尾追加："
                "<code>?token=你的令牌</code></p></body></html>"
            ).encode("utf-8")
        else:
            body = json.dumps({
                "error": {
                    "code": "FORBIDDEN",
                    "message": "需要访问令牌",
                    "hint": "请在 URL 后附加 ?token=... 或携带 X-Access-Token 请求头",
                }
            }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", (b"text/html; charset=utf-8" if b"text/html" in accept else b"application/json")),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app = FastAPI(title="Pixiv Archive")
app.add_middleware(LANGuardMiddleware)

THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "thumbnails")
THUMBNAIL_PATH = os.path.join(paths.DATA_DIR, THUMBNAIL_DIR)
SOURCE_DIR_SEPARATOR = "|"

# 自定义 ASGI 服务器不会触发 FastAPI 的 lifespan/startup 事件，
# 这里在模块加载时显式建库建表，确保全新环境可直接使用。
init_db()

os.makedirs(THUMBNAIL_PATH, exist_ok=True)
app.mount("/thumbnails", StaticFiles(directory=THUMBNAIL_PATH), name="thumbnails")

static_dir = os.path.join(paths.APP_DIR, "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

templates_dir = os.path.join(paths.APP_DIR, "templates")
templates = Jinja2Templates(directory=templates_dir)


def _split_source_dirs(raw):
    return [(p or "").strip() for p in (raw or "").split(SOURCE_DIR_SEPARATOR) if (p or "").strip()]


def _get_source_dirs():
    dirs = _split_source_dirs(os.getenv("IMAGE_SOURCE_DIRS", ""))
    legacy = (os.getenv("IMAGE_SOURCE_DIR", "") or "").strip()
    if legacy:
        dirs.insert(0, legacy)
    seen = set()
    result = []
    for directory in dirs:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen:
            seen.add(key)
            result.append(directory)
    return result


def _primary_source_dir():
    dirs = _get_source_dirs()
    return dirs[0] if dirs else ""


def _auto_watch_enabled():
    return (os.getenv("AUTO_WATCH_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _scan_source_dirs(source_dirs, progress_callback=None, cancel_event=None, pause_event=None, artwork_callback=None, incremental=False, failure_callback=None, initialize_db=True, changed_files=None):
    scan_mode = "incremental scan" if incremental else "scan"
    log.info("%s started for %d source director%s: %s",
             scan_mode, len(source_dirs), "y" if len(source_dirs) == 1 else "ies", source_dirs)
    combined = {
        "total_files_scanned": 0,
        "pixiv_artworks_found": 0,
        "new_artworks": 0,
        "new_images": 0,
        "skipped": 0,
        "duplicates": 0,
        "pruned_duplicates": 0,
        "pruned_images": 0,
        "pruned_artworks": 0,
        "source_dirs": source_dirs,
    }
    for idx, source_dir in enumerate(source_dirs):
        if cancel_event and cancel_event.is_set():
            combined["cancelled"] = True
            break
        if progress_callback:
            progress_callback("scan", idx + 1, len(source_dirs), f"扫描图片目录 {idx + 1}/{len(source_dirs)}")
        source_changed_files = None
        if changed_files is not None:
            source_root = os.path.normcase(os.path.abspath(source_dir))
            source_changed_files = []
            for path in changed_files:
                absolute = os.path.normcase(os.path.abspath(path))
                try:
                    if os.path.commonpath((source_root, absolute)) == source_root:
                        source_changed_files.append(path)
                except ValueError:
                    continue
            if not source_changed_files:
                continue
        result = scan_directory(
            source_dir,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            pause_event=pause_event,
            artwork_callback=artwork_callback,
            incremental=incremental,
            failure_callback=failure_callback,
            initialize_db=initialize_db and idx == 0,
            changed_files=source_changed_files,
        )
        log.info(
            "%s directory finished: %s; files=%s artworks=%s new_artworks=%s new_images=%s skipped=%s duplicates=%s",
            scan_mode,
            source_dir,
            result.get("total_files_scanned", 0),
            result.get("pixiv_artworks_found", 0),
            result.get("new_artworks", 0),
            result.get("new_images", 0),
            result.get("skipped", 0),
            result.get("duplicates", 0),
        )
        if result.get("error"):
            raise RuntimeError(result["error"])
        if result.get("cancelled"):
            combined["cancelled"] = True
        for key in (
            "total_files_scanned", "pixiv_artworks_found", "new_artworks", "new_images",
            "skipped", "duplicates", "pruned_duplicates", "pruned_images", "pruned_artworks",
        ):
            combined[key] += result.get(key, 0) or 0
    log.info("%s finished: %s", scan_mode, combined)
    return combined


def _run_ingest_pipeline(job, source_dirs, incremental=False, changed_files=None):
    # Schema 初始化只做一次，避免并行流水线重复触发 WAL/迁移检查。
    init_db()
    result = {
        "total_files_scanned": 0,
        "pixiv_artworks_found": 0,
        "new_artworks": 0,
        "new_images": 0,
        "skipped": 0,
        "duplicates": 0,
        "pruned_duplicates": 0,
        "pruned_images": 0,
        "pruned_artworks": 0,
        "source_dirs": source_dirs,
        "changed_artwork_ids": [],
        "changed_pixiv_ids": [],
        "thumbnails": {"generated": 0, "skipped": 0, "failed": 0, "cancelled": False},
        "synced": 0,
        "ai_detected": 0,
        "sync_failed": 0,
        "sync_deleted": 0,
    }
    has_token = bool(os.getenv("PIXIV_REFRESH_TOKEN", ""))
    progress_lock = threading.Lock()
    state_lock = threading.Lock()
    thumb_queue = Queue(maxsize=64)
    sync_queue = Queue(maxsize=64)
    scan_finished = threading.Event()
    sync_disabled = threading.Event()
    counts = {
        "seen": 0,
        "thumb_total": 0,
        "thumb_done": 0,
        "sync_total": 0,
        "sync_done": 0,
    }
    fail_pause_enabled = _bool_env(os.getenv("AUTO_FAIL_PAUSE_ENABLED", ""), True)
    fail_pause_threshold = _parse_fail_pause_threshold(os.getenv("AUTO_FAIL_PAUSE_THRESHOLD", ""), _FAIL_PAUSE_DEFAULT)
    try:
        sync_batch_size = max(1, min(int(os.getenv("PA_SYNC_BATCH_SIZE", "8")), 32))
    except ValueError:
        sync_batch_size = 8
    failure_streak = {"scan": 0, "thumb": 0, "sync": 0}

    def update_job(phase=None, current=None, total=None, message=None):
        with progress_lock:
            job.update(phase=phase, current=current, total=total, message=message)

    def note_failure(stage, label, detail):
        if not fail_pause_enabled or stage not in failure_streak:
            return
        failure_streak[stage] += 1
        streak = failure_streak[stage]
        log.warning("%s failure streak %d/%d: %s (%s)", stage, streak, fail_pause_threshold, label, detail)
        if streak >= fail_pause_threshold and not job.pause_event.is_set():
            job.pause()
            update_job(phase=stage, message=f"{stage} 连续失败 {streak} 次，已自动暂停")
            log.warning("job auto-paused after consecutive %s failures: id=%s threshold=%d", stage, job.job_id, fail_pause_threshold)

    def note_success(stage):
        if stage in failure_streak:
            failure_streak[stage] = 0

    def queue_put(q, item, disabled_event=None):
        while not job.cancel_event.is_set():
            if disabled_event is not None and disabled_event.is_set():
                return False
            try:
                q.put(item, timeout=0.5)
                return True
            except Full:
                job.wait_if_paused()
        return False

    def artwork_callback(payload):
        if job.cancel_event.is_set():
            return
        needs_thumbnail = bool(payload.get("needs_thumbnail"))
        needs_sync = bool(payload.get("needs_sync")) and has_token and not sync_disabled.is_set()
        with state_lock:
            counts["seen"] += 1
            seen = counts["seen"]
            if needs_thumbnail:
                counts["thumb_total"] += 1
            if needs_sync:
                counts["sync_total"] += 1
            if needs_thumbnail or needs_sync:
                result["changed_artwork_ids"].append(payload["artwork_id"])
                result["changed_pixiv_ids"].append(payload["pixiv_id"])
            thumb_total = counts["thumb_total"]
            sync_total = counts["sync_total"]
        update_job("scan", seen, None, f"扫描中…已发现 {seen} 个作品")
        if needs_thumbnail and queue_put(thumb_queue, payload):
            with state_lock:
                thumb_total = counts["thumb_total"]
            update_job("thumb", counts["thumb_done"], thumb_total or None, f"准备缩略图任务…{counts['thumb_done']}/{thumb_total}")
        if needs_sync:
            queued = queue_put(sync_queue, payload, sync_disabled)
            with state_lock:
                if not queued:
                    counts["sync_total"] = max(0, counts["sync_total"] - 1)
                sync_total = counts["sync_total"]
            if queued:
                update_job("sync", counts["sync_done"], sync_total or None, f"准备元数据任务…{counts['sync_done']}/{sync_total}")

    def thumb_worker():
        while True:
            try:
                item = thumb_queue.get(timeout=0.5)
            except Empty:
                if job.cancel_event.is_set():
                    return
                if scan_finished.is_set():
                    with state_lock:
                        if counts["thumb_done"] >= counts["thumb_total"]:
                            return
                continue
            if job.cancel_event.is_set():
                return
            job.wait_if_paused()
            if job.cancel_event.is_set():
                return
            with state_lock:
                counts["thumb_done"] += 1
                done = counts["thumb_done"]
                total = counts["thumb_total"]
            update_job("thumb", done, total or None, f"生成缩略图…{done}/{total}（PID {item['pixiv_id']}）")
            try:
                thumb_path = generate_thumbnail(
                    item["cover_path"],
                    item["pixiv_id"],
                    force=bool(item.get("cover_changed")),
                )
            except Exception as e:
                thumb_path = None
                note_failure("thumb", item["pixiv_id"], str(e))
                log.exception("thumbnail generation failed: pid=%s", item["pixiv_id"])
            if thumb_path:
                try:
                    if os.path.getsize(thumb_path) > 0:
                        result["thumbnails"]["generated"] += 1
                        note_success("thumb")
                    else:
                        result["thumbnails"]["failed"] += 1
                        note_failure("thumb", item["pixiv_id"], "empty-thumbnail")
                except Exception:
                    result["thumbnails"]["failed"] += 1
                    note_failure("thumb", item["pixiv_id"], "thumbnail-stat")
            else:
                result["thumbnails"]["failed"] += 1
                note_failure("thumb", item["pixiv_id"], "thumbnail-create")

    def sync_worker():
        while True:
            try:
                item = sync_queue.get(timeout=0.5)
            except Empty:
                if job.cancel_event.is_set():
                    return
                if scan_finished.is_set():
                    with state_lock:
                        if counts["sync_done"] >= counts["sync_total"]:
                            return
                continue
            if job.cancel_event.is_set():
                return
            job.wait_if_paused()
            if job.cancel_event.is_set():
                return
            batch = [item]
            while len(batch) < sync_batch_size:
                try:
                    batch.append(sync_queue.get_nowait())
                except Empty:
                    break
            with state_lock:
                done = counts["sync_done"]
                total = counts["sync_total"]
            update_job(
                "sync",
                done,
                total or None,
                f"同步元数据…{done}/{total}（批次 {len(batch)} 个作品）",
            )
            sync_result = sync_metadata(
                specific_pixiv_ids=[entry["pixiv_id"] for entry in batch],
                progress_callback=None,
                cancel_event=job.cancel_event,
                pause_event=job.pause_event,
                failure_callback=lambda stage, label, detail: note_failure("sync", label, detail),
                initialize_db=False,
                commit_each=False,
            )
            with state_lock:
                counts["sync_done"] += len(batch)
                result["synced"] += sync_result.get("synced", 0) or 0
                result["ai_detected"] += sync_result.get("ai_detected", 0) or 0
                result["sync_failed"] += sync_result.get("failed", 0) or 0
                result["sync_deleted"] += sync_result.get("deleted", 0) or 0
                result["thumbnails"]["cancelled"] = bool(result["thumbnails"].get("cancelled")) or bool(sync_result.get("cancelled"))
                if sync_result.get("auth_error"):
                    result["sync_auth_error"] = sync_result.get("auth_error")
                if sync_result.get("details"):
                    result.setdefault("sync_details", []).extend(sync_result["details"])
            if sync_result.get("auth_error"):
                sync_disabled.set()
                log.warning(
                    "metadata pipeline disabled after authentication failure; "
                    "remaining artworks will be retried by the next sync"
                )
                return
            publish(
                "metadata_batch_done",
                {
                    "job_id": job.job_id,
                    "pixiv_ids": [entry["pixiv_id"] for entry in batch],
                    "synced": sync_result.get("synced", 0),
                    "ai_detected": sync_result.get("ai_detected", 0),
                    "failed": sync_result.get("failed", 0),
                },
            )
            if not sync_result.get("failed", 0):
                note_success("sync")

    thumb_thread = threading.Thread(target=thumb_worker, name="thumb-worker", daemon=True)
    sync_thread = threading.Thread(target=sync_worker, name="sync-worker", daemon=True) if has_token else None
    thumb_thread.start()
    if sync_thread:
        sync_thread.start()

    try:
        job.update(phase="scan", message="开始扫描…")
        scan_result = _scan_source_dirs(
            source_dirs,
            progress_callback=update_job,
            cancel_event=job.cancel_event,
            pause_event=job.pause_event,
            artwork_callback=artwork_callback,
            incremental=incremental,
            failure_callback=lambda stage, label, detail: note_failure("scan", label, detail),
            initialize_db=False,
            changed_files=changed_files,
        )
        for key in (
            "total_files_scanned", "pixiv_artworks_found", "new_artworks", "new_images",
            "skipped", "duplicates", "pruned_duplicates", "pruned_images", "pruned_artworks",
        ):
            result[key] = scan_result.get(key, 0) or 0
        result["source_dirs"] = scan_result.get("source_dirs", source_dirs)
        result["cancelled"] = bool(scan_result.get("cancelled")) or job.cancel_event.is_set()
        if not has_token:
            result["sync_skipped"] = "NO_TOKEN"
    finally:
        scan_finished.set()
        thumb_thread.join()
        if sync_thread:
            sync_thread.join()
        result["thumbnails"]["cancelled"] = bool(result["thumbnails"].get("cancelled")) or job.cancel_event.is_set()
        result["sync"] = {
            "synced": result["synced"],
            "ai_detected": result["ai_detected"],
            "failed": result["sync_failed"],
            "deleted": result["sync_deleted"],
            "auth_error": result.get("sync_auth_error"),
            "details": result.get("sync_details", []),
        }
        job.state["result"] = result

    return result


def _run_scan_for_sources(job, source_dirs, changed_files=None):
    pipeline_result = _run_ingest_pipeline(
        job,
        source_dirs,
        incremental=True,
        changed_files=changed_files,
    )
    return pipeline_result


def _start_auto_scan(changed_files=None):
    global _last_auto_job_id
    changed_files = list(dict.fromkeys(changed_files or [])) or None
    source_dirs = [d for d in _get_source_dirs() if os.path.isdir(d)]
    if not source_dirs:
        log.warning("auto scan skipped: no accessible source directories from configured paths %s", _get_source_dirs())
        return

    def run_scan(job):
        publish("auto_scan_started", {"job_id": job.job_id, "source_dirs": source_dirs})
        try:
            if changed_files:
                log.info("auto scan using watchdog file list: files=%d", len(changed_files))
            _run_scan_for_sources(job, source_dirs, changed_files=changed_files)
            publish("auto_scan_done", {"job_id": job.job_id, "result": job.state.get("result") or {}})
        except Exception as e:
            publish("auto_scan_failed", {"job_id": job.job_id, "error": str(e)})
            raise

    job_id, error = jobs.start("auto_scan", run_scan)
    if error:
        log.info("auto scan delayed because another job is running: %s", error)
        _folder_watcher.schedule_scan(changed_files or [], "reschedule")
    else:
        _last_auto_job_id = job_id
        log.info("auto scan job started: id=%s", job_id)
        publish("auto_scan_job_created", {"job_id": job_id})


_folder_watcher = FolderWatcher(_start_auto_scan)
_last_auto_job_id = None
_bookmark_auto_thread = None
_bookmark_auto_stop = threading.Event()
_bookmark_auto_last_run = 0.0


def _restart_folder_watcher():
    if _auto_watch_enabled():
        result = _folder_watcher.restart(_get_source_dirs())
        log.info("auto watch enabled; restart result: %s", result)
        return result
    _folder_watcher.stop()
    log.info("auto watch disabled")
    return {"ok": True, "paths": []}


def _bookmark_auto_enabled():
    settings = _read_env_file()
    return _bool_env(settings.get("BOOKMARK_AUTO_CHECK_ENABLED", ""), False)


def _bookmark_auto_schedule():
    settings = _read_env_file()
    return {
        "weekday": _parse_weekday(settings.get("BOOKMARK_AUTO_CHECK_WEEKDAY", ""), 0),
        "time": _parse_hhmm(settings.get("BOOKMARK_AUTO_CHECK_TIME", ""), "03:30"),
    }


def _bookmark_auto_due(now=None):
    schedule = _bookmark_auto_schedule()
    lt = time.gmtime((now or time.time()) + 8 * 3600)
    hh, mm = [int(x) for x in schedule["time"].split(":", 1)]
    if lt.tm_wday != schedule["weekday"]:
        return False, schedule, ""
    if lt.tm_hour != hh or lt.tm_min != mm:
        return False, schedule, ""
    run_key = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}-{hh:02d}:{mm:02d}"
    return True, schedule, run_key


def _restart_bookmark_scheduler():
    global _bookmark_auto_thread
    if _bookmark_auto_thread and _bookmark_auto_thread.is_alive():
        _bookmark_auto_stop.set()
        _bookmark_auto_thread.join(timeout=2)
    _bookmark_auto_stop.clear()

    if not _bookmark_auto_enabled():
        log.info("bookmark auto scheduler disabled")
        _bookmark_auto_thread = None
        return {"ok": True, "enabled": False}

    def _loop():
        global _bookmark_auto_last_run
        log.info("bookmark auto scheduler started")
        while not _bookmark_auto_stop.wait(20):
            if not _bookmark_auto_enabled():
                continue
            due, schedule, run_key = _bookmark_auto_due()
            if not due:
                continue
            if jobs.is_busy():
                continue
            with get_db() as conn:
                enabled_count = conn.execute(
                    "SELECT COUNT(*) FROM bookmark_subs WHERE auto_download = 1"
                ).fetchone()[0]
            if not enabled_count:
                continue
            if _bookmark_auto_last_run == run_key:
                continue
            log.info("bookmark auto scheduler triggering check: weekday=%s time=%s timezone=Asia/Shanghai",
                     schedule["weekday"], schedule["time"])
            job_id, err = _launch_bookmark_check_all()
            if job_id:
                _bookmark_auto_last_run = run_key
                log.info("bookmark auto scheduler launched job: id=%s", job_id)
            elif err:
                log.info("bookmark auto scheduler skipped: prerequisites not met or job busy")

    _bookmark_auto_thread = threading.Thread(target=_loop, name="bookmark-auto-scheduler", daemon=True)
    _bookmark_auto_thread.start()
    schedule = _bookmark_auto_schedule()
    return {"ok": True, "enabled": True, "weekday": schedule["weekday"], "time": schedule["time"]}


def _normalize_existing_pages():
    log.info("normalizing existing image pages")
    with get_db() as conn:
        _normalize_image_pages(conn)
    log.info("existing image pages normalized")


def _sync_after_scan_if_needed(job, result):
    changed = (result.get("new_artworks", 0) or 0) > 0 or (result.get("new_images", 0) or 0) > 0
    if not changed or job.cancel_event.is_set():
        return
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        log.warning("metadata sync skipped after scan: PIXIV_REFRESH_TOKEN is not set")
        result["sync_skipped"] = "NO_TOKEN"
        return
    job.update(phase="sync", current=0, total=None, message="扫描完成，正在同步新作品元数据…")
    log.info("metadata sync after scan started")
    try:
        batch_size = max(1, min(int(os.getenv("PA_SYNC_BATCH_SIZE", "8")), 32))
    except (TypeError, ValueError):
        batch_size = 8
    sync_result = sync_metadata(
        progress_callback=job.update,
        cancel_event=job.cancel_event,
        commit_each=False,
        commit_batch_size=batch_size,
    )
    result["synced"] = sync_result.get("synced", 0)
    result["ai_detected"] = sync_result.get("ai_detected", 0)
    result["sync_failed"] = sync_result.get("failed", 0)
    result["sync_deleted"] = sync_result.get("deleted", 0)
    log.info("metadata sync after scan finished: %s", sync_result)


@app.on_event("startup")
def startup():
    init_db()
    _normalize_existing_pages()
    _restart_folder_watcher()
    _restart_bookmark_scheduler()


_normalize_existing_pages()
_restart_folder_watcher()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


# ---- R18 / 状态筛选 ----
# 严格判定：#R18 标签（R-18 / R18 / 18R，忽略首尾空格和大小写），不含 R-18G。
_R18_TAG_SET = "(" + ",".join("'" + name + "'" for name in sorted(R18_TAG_NAMES)) + ")"


def _r18_exists(alias="a"):
    return (
        f"{alias}.id IN (SELECT rj.artwork_id FROM artwork_tags rj "
        f"JOIN tags rt ON rj.tag_id = rt.id "
        f"WHERE (LOWER(TRIM(COALESCE(rt.name, ''))) IN {_R18_TAG_SET} "
        f"OR LOWER(TRIM(COALESCE(rt.translated_name, ''))) IN {_R18_TAG_SET}))"
    )


def _r18_where(alias, value):
    mode = (value or "").strip().lower()
    if mode == "hide":
        return "NOT " + _r18_exists(alias)
    if mode == "only":
        return "(" + _r18_exists(alias) + ")"
    return None


def _ai_exists(alias="a"):
    """Pixiv official AI marker: ai_type=2."""
    return f"COALESCE({alias}.ai_type, 0) = 2"


def _ai_where(alias, value):
    mode = (value or "").strip().lower()
    if mode == "hide":
        return "NOT (" + _ai_exists(alias) + ")"
    if mode == "only":
        return "(" + _ai_exists(alias) + ")"
    return None


def _cover_image_join(alias="a"):
    return (
        f"LEFT JOIN images i ON i.id = ("
        f"SELECT i2.id FROM images i2 "
        f"WHERE i2.artwork_id = {alias}.id "
        f"ORDER BY CASE WHEN i2.page = 1 THEN 0 ELSE 1 END, "
        f"i2.page ASC, i2.id ASC LIMIT 1)"
    )


def _gallery_row_payload(row, expand_pages=False):
    item = {
        "id": row["id"],
        "pixiv_id": row["pixiv_id"],
        "title": row["title"] or f"Pixiv ID: {row['pixiv_id']}",
        "author_name": row["author_name"] or "",
        "page_count": row["page_count"],
        "create_date": row["create_date"] or "",
        "pixiv_status": row["pixiv_status"],
        "thumb_path": row["thumb_path"] or "",
        "is_favorited": bool(row["is_favorited"]),
        "is_r18": bool(row["is_r18"]),
        "is_ai": int(row["ai_type"] or 0) == 2,
        "sync_error": row["sync_error"] or "",
    }
    if expand_pages:
        item.update({
            "image_id": row["image_id"],
            "image_page": row["image_page"],
            "image_path": row["image_path"] or "",
        })
    return item


def _attach_gallery_images(conn, artworks):
    artwork_ids = [a["id"] for a in artworks]
    if not artwork_ids:
        return artworks
    placeholders = ",".join("?" for _ in artwork_ids)
    rows = conn.execute(
        f"""SELECT id, artwork_id, page, path
            FROM images
            WHERE artwork_id IN ({placeholders})
            ORDER BY artwork_id ASC, page ASC, id ASC""",
        artwork_ids,
    ).fetchall()
    grouped = {artwork_id: [] for artwork_id in artwork_ids}
    for row in rows:
        grouped.setdefault(row["artwork_id"], []).append({
            "id": row["id"],
            "page": row["page"],
            "path": row["path"] or "",
        })
    for artwork in artworks:
        artwork["images"] = grouped.get(artwork["id"], [])
    return artworks


@app.get("/api/artworks")
def api_artworks(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("id", pattern="^(id|pixiv_id|title|create_date|first_seen|random)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    author: str = Query(""),
    tag: str = Query(""),
    favorite_id: int = Query(0, ge=0),
    r18: str = Query(""),
    ai: str = Query(""),
    status: str = Query(""),
    expand_pages: bool = Query(False),
    group_pages: bool = Query(False),
):
    with get_db() as conn:
        where_clauses = []
        params = []

        if author:
            where_clauses.append("a.author_name LIKE ?")
            params.append(f"%{author}%")
        if tag:
            where_clauses.append(
                "a.id IN (SELECT artwork_id FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id WHERE t.name LIKE ?)"
            )
            params.append(f"%{tag}%")
        if favorite_id:
            where_clauses.append(
                "a.id IN (SELECT artwork_id FROM favorite_artworks "
                "WHERE favorite_id = ?)"
            )
            params.append(favorite_id)
        if status == "deleted":
            where_clauses.append("a.pixiv_status = 'deleted'")
        _r18c = _r18_where("a", r18)
        if _r18c:
            where_clauses.append(_r18c)
        _aic = _ai_where("a", ai)
        if _aic:
            where_clauses.append(_aic)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)
        if expand_pages and not group_pages:
            count_sql = f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id {where_sql}"
        else:
            count_sql = f"SELECT COUNT(*) FROM artworks a {where_sql}"
        count_row = conn.execute(count_sql, params).fetchone()
        total = count_row[0]
        total_images = None
        if group_pages:
            total_images = conn.execute(
                f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id {where_sql}",
                params,
            ).fetchone()[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = "ORDER BY RANDOM()"
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        if expand_pages and not group_pages:
            if sort != "random":
                order_sql += ", i.page ASC, i.id ASC"
            rows = conn.execute(
                f"""SELECT a.*, i.id AS image_id, i.page AS image_page,
                    i.path AS image_path, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    JOIN images i ON i.artwork_id = a.id
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT a.*, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    {_cover_image_join()}
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

        artworks = [_gallery_row_payload(row, expand_pages and not group_pages) for row in rows]
        if group_pages:
            _attach_gallery_images(conn, artworks)

        result = {
            "artworks": artworks,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }
        if total_images is not None:
            result["total_images"] = total_images
        return result


@app.get("/api/artworks/{artwork_id}")
def api_artwork_detail(artwork_id: int):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM artworks WHERE id = ?", (artwork_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, status_code=404)

        images = conn.execute(
            "SELECT * FROM images WHERE artwork_id = ? ORDER BY page",
            (artwork_id,),
        ).fetchall()
        image_payload = []
        for img in images:
            payload = dict(img)
            stat_info = _format_local_time_from_path(payload["path"])
            payload["file_created_at"] = stat_info["created"]
            payload["file_modified_at"] = stat_info["modified"]
            image_payload.append(payload)

        tags = conn.execute(
            """SELECT t.* FROM tags t
               JOIN artwork_tags at2 ON t.id = at2.tag_id
               WHERE at2.artwork_id = ?""",
            (artwork_id,),
        ).fetchall()

        favorites = conn.execute(
            """SELECT f.id, f.name FROM favorites f
               JOIN favorite_artworks fa ON fa.favorite_id = f.id
               WHERE fa.artwork_id = ?
               ORDER BY f.name""",
            (artwork_id,),
        ).fetchall()

        return {
            "id": row["id"],
            "pixiv_id": row["pixiv_id"],
            "title": row["title"] or f"Pixiv ID: {row['pixiv_id']}",
            "description": row["description"] or "",
            "author_id": row["author_id"],
            "author_name": row["author_name"] or "",
            "create_date": row["create_date"] or "",
            "page_count": row["page_count"],
            "width": row["width"],
            "height": row["height"],
            "pixiv_status": row["pixiv_status"],
            "first_seen": row["first_seen"],
            "last_synced": row["last_synced"],
            "sync_error": row["sync_error"] or "",
            "images": image_payload,
            "tags": [dict(tag) for tag in tags],
            "favorites": [dict(f) for f in favorites],
        }


def _remove_artwork_files(paths_to_remove):
    """Remove source files before deleting their DB rows.

    Missing files are already in the desired state and are therefore
    tolerated. Permission and filesystem errors are returned so callers do
    not report a successful deletion while leaving a live source file behind.
    """
    errors = []
    for path in paths_to_remove:
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError as exc:
            errors.append({"path": path, "error": str(exc)})
    return errors


@app.delete("/api/artworks/{artwork_id}")
def api_delete_artwork(artwork_id: int, delete_files: bool = Query(False)):
    with get_db() as conn:
        images = conn.execute(
            "SELECT path FROM images WHERE artwork_id = ?", (artwork_id,)
        ).fetchall()

        pixiv_id = conn.execute(
            "SELECT pixiv_id FROM artworks WHERE id = ?", (artwork_id,)
        ).fetchone()

        if not pixiv_id:
            return JSONResponse({"error": "Not found"}, status_code=404)

        if delete_files:
            file_errors = _remove_artwork_files([img["path"] for img in images])
            if file_errors:
                return JSONResponse(
                    {
                        "error": "源文件删除失败，已保留数据库记录",
                        "files_deleted": False,
                        "file_errors": file_errors,
                    },
                    status_code=409,
                )

        conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
        conn.execute("DELETE FROM images WHERE artwork_id = ?", (artwork_id,))
        conn.execute("DELETE FROM artworks WHERE id = ?", (artwork_id,))

        if delete_files:
            thumb_path = os.path.join(THUMBNAIL_PATH, f"{pixiv_id['pixiv_id']}.jpg")
            try:
                os.remove(thumb_path)
            except Exception:
                pass

        return {"status": "deleted", "artwork_id": artwork_id, "files_deleted": delete_files}


@app.post("/api/artworks/batch-delete")
async def api_batch_delete_artworks(request: Request):
    from pydantic import BaseModel

    class BatchDelete(BaseModel):
        artwork_ids: list[int]
        delete_files: bool = False

    body = await request.json()
    data = BatchDelete(**body)
    deleted = 0
    with get_db() as conn:
        for artwork_id in data.artwork_ids:
            images = conn.execute(
                "SELECT path FROM images WHERE artwork_id = ?", (artwork_id,)
            ).fetchall()
            pixiv_id = conn.execute(
                "SELECT pixiv_id FROM artworks WHERE id = ?", (artwork_id,)
            ).fetchone()
            if not pixiv_id:
                continue
            if data.delete_files:
                file_errors = _remove_artwork_files([img["path"] for img in images])
                if file_errors:
                    log.warning(
                        "artwork batch delete kept DB row because source removal failed: "
                        "artwork_id=%s errors=%s",
                        artwork_id,
                        file_errors,
                    )
                    continue
            conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
            conn.execute("DELETE FROM images WHERE artwork_id = ?", (artwork_id,))
            conn.execute("DELETE FROM artworks WHERE id = ?", (artwork_id,))
            if data.delete_files:
                thumb_path = os.path.join(THUMBNAIL_PATH, f"{pixiv_id['pixiv_id']}.jpg")
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass
            deleted += 1
    return {"status": "ok", "deleted": deleted, "files_deleted": data.delete_files}


@app.post("/api/artworks/batch-favorites-clear")
async def api_batch_favorites_clear(request: Request):
    from pydantic import BaseModel

    class BatchClear(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = BatchClear(**body)
    with get_db() as conn:
        if data.artwork_ids:
            placeholders = ",".join("?" * len(data.artwork_ids))
            conn.execute(
                f"DELETE FROM favorite_artworks WHERE artwork_id IN ({placeholders})",
                data.artwork_ids,
            )
    return {"status": "ok", "cleared": len(data.artwork_ids)}


@app.get("/api/search")
def api_search(
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("id", pattern="^(id|pixiv_id|title|create_date|first_seen|random)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    author: str = Query(""),
    tag: str = Query(""),
    favorite_id: int = Query(0, ge=0),
    r18: str = Query(""),
    ai: str = Query(""),
    status: str = Query(""),
    expand_pages: bool = Query(False),
    group_pages: bool = Query(False),
):
    query = q.strip()
    if not query:
        return api_artworks(page=page, per_page=per_page, sort=sort, order=order,
                            author=author, tag=tag, favorite_id=favorite_id,
                            r18=r18, status=status, expand_pages=expand_pages,
                            group_pages=group_pages, ai=ai)

    tokens = [t for t in query.split() if t]

    with get_db() as conn:
        token_conditions = []
        params = []
        for token in tokens:
            like = f"%{token}%"
            cond = (
                "(a.title LIKE ? OR a.description LIKE ? OR a.author_name LIKE ? "
                "OR CAST(a.pixiv_id AS TEXT) LIKE ? "
                "OR EXISTS (SELECT 1 FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id "
                "WHERE at2.artwork_id = a.id "
                "AND (t.name LIKE ? OR t.translated_name LIKE ?)))"
            )
            token_conditions.append(cond)
            params.extend([like, like, like, like, like, like])

        if author:
            token_conditions.append("a.author_name LIKE ?")
            params.append(f"%{author}%")
        if tag:
            token_conditions.append(
                "a.id IN (SELECT artwork_id FROM artwork_tags at2 "
                "JOIN tags t ON at2.tag_id = t.id WHERE t.name LIKE ?)"
            )
            params.append(f"%{tag}%")
        if favorite_id:
            token_conditions.append(
                "a.id IN (SELECT artwork_id FROM favorite_artworks "
                "WHERE favorite_id = ?)"
            )
            params.append(favorite_id)
        if status == "deleted":
            token_conditions.append("a.pixiv_status = 'deleted'")
        _r18c = _r18_where("a", r18)
        if _r18c:
            token_conditions.append(_r18c)
        _aic = _ai_where("a", ai)
        if _aic:
            token_conditions.append(_aic)

        where_sql = " WHERE " + " AND ".join(token_conditions)

        if expand_pages and not group_pages:
            count_sql = f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id{where_sql}"
        else:
            count_sql = f"SELECT COUNT(*) FROM artworks a{where_sql}"
        count_row = conn.execute(count_sql, params).fetchone()
        total = count_row[0]
        total_images = None
        if group_pages:
            total_images = conn.execute(
                f"SELECT COUNT(*) FROM artworks a JOIN images i ON i.artwork_id = a.id{where_sql}",
                params,
            ).fetchone()[0]

        offset = (page - 1) * per_page
        if sort == "random":
            order_sql = "ORDER BY RANDOM()"
        else:
            sort_column = {
                "id": "a.id", "pixiv_id": "a.pixiv_id",
                "title": "a.title", "create_date": "a.create_date",
                "first_seen": "a.first_seen",
            }[sort]
            order_direction = "DESC" if order == "desc" else "ASC"
            order_sql = f"ORDER BY {sort_column} {order_direction}"

        if expand_pages and not group_pages:
            if sort != "random":
                order_sql += ", i.page ASC, i.id ASC"
            rows = conn.execute(
                f"""SELECT a.*, i.id AS image_id, i.page AS image_page,
                    i.path AS image_path, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    JOIN images i ON i.artwork_id = a.id
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT a.*, i.path AS thumb_path,
                    a.id IN (SELECT fa.artwork_id FROM favorite_artworks fa) AS is_favorited,
                    {_r18_exists()} AS is_r18
                    FROM artworks a
                    {_cover_image_join()}
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

        artworks = [_gallery_row_payload(row, expand_pages and not group_pages) for row in rows]
        if group_pages:
            _attach_gallery_images(conn, artworks)

        result = {
            "artworks": artworks,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
        }
        if total_images is not None:
            result["total_images"] = total_images
        return result


@app.get("/api/tags")
def api_tags(q: str = Query("")):
    with get_db() as conn:
        q = q.strip()
        where = ""
        params = []
        if q:
            where = " WHERE t.name LIKE ?"
            params.append(f"%{q}%")
        rows = conn.execute(
            f"""SELECT t.*, COUNT(at2.artwork_id) AS artwork_count
               FROM tags t
               LEFT JOIN artwork_tags at2 ON t.id = at2.tag_id
               {where}
               GROUP BY t.id
               ORDER BY artwork_count DESC
               LIMIT 200""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/favorites")
def api_favorites(r18: str = Query("")):
    with get_db() as conn:
        favorites = conn.execute(
            """SELECT f.*, COUNT(fa.artwork_id) AS artwork_count
               FROM favorites f
               LEFT JOIN favorite_artworks fa ON fa.favorite_id = f.id
               GROUP BY f.id
               ORDER BY f.create_date DESC, f.id DESC"""
        ).fetchall()

        result = []
        for fav in favorites:
            works_clauses = ["fa.favorite_id = ?"]
            _r18c = _r18_where("a", r18)
            if _r18c:
                works_clauses.append(_r18c)
            works = conn.execute(
                f"""SELECT a.id, a.pixiv_id, a.title, a.page_count, i.path AS thumb_path
                    FROM artworks a
                    {_cover_image_join()}
                    JOIN favorite_artworks fa ON fa.artwork_id = a.id
                    WHERE {" AND ".join(works_clauses)}
                    ORDER BY fa.added_date DESC
                    LIMIT 12""",
                (fav["id"],),
            ).fetchall()
            result.append({
                "id": fav["id"],
                "name": fav["name"],
                "create_date": fav["create_date"],
                "artwork_count": fav["artwork_count"],
                "works": [dict(w) for w in works],
            })
        return result


@app.post("/api/favorites")
async def api_create_favorite(request: Request):
    from pydantic import BaseModel

    class FavoriteCreate(BaseModel):
        name: str

    body = await request.json()
    data = FavoriteCreate(**body)
    name = data.name.strip()
    if not name:
        return JSONResponse({"error": "名称不能为空"}, status_code=400)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM favorites WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            return JSONResponse({"error": "收藏夹已存在"}, status_code=400)
        cursor = conn.execute(
            "INSERT INTO favorites (name) VALUES (?)", (name,)
        )
        return {"status": "ok", "id": cursor.lastrowid, "name": name}


@app.delete("/api/favorites/{favorite_id}")
def api_delete_favorite(favorite_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM favorites WHERE id = ?", (favorite_id,))
        return {"status": "deleted"}


@app.post("/api/favorites/{favorite_id}/artworks")
async def api_add_favorite_artworks(favorite_id: int, request: Request):
    from pydantic import BaseModel

    class FavoriteAdd(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = FavoriteAdd(**body)
    with get_db() as conn:
        fav = conn.execute(
            "SELECT id FROM favorites WHERE id = ?", (favorite_id,)
        ).fetchone()
        if not fav:
            return JSONResponse({"error": "收藏夹不存在"}, status_code=404)
        added = 0
        for aid in data.artwork_ids:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO favorite_artworks (favorite_id, artwork_id) VALUES (?, ?)",
                (favorite_id, aid),
            )
            if cursor.rowcount:
                added += 1
        return {"status": "ok", "added": added}


@app.delete("/api/favorites/{favorite_id}/artworks/{artwork_id}")
def api_remove_favorite_artwork(favorite_id: int, artwork_id: int):
    with get_db() as conn:
        conn.execute(
            "DELETE FROM favorite_artworks WHERE favorite_id = ? AND artwork_id = ?",
            (favorite_id, artwork_id),
        )
        return {"status": "ok"}


@app.post("/api/favorites/{favorite_id}/artworks/batch-remove")
async def api_batch_remove_favorite_artworks(favorite_id: int, request: Request):
    from pydantic import BaseModel

    class BatchRemove(BaseModel):
        artwork_ids: list[int]

    body = await request.json()
    data = BatchRemove(**body)
    with get_db() as conn:
        if data.artwork_ids:
            placeholders = ",".join("?" * len(data.artwork_ids))
            conn.execute(
                f"DELETE FROM favorite_artworks WHERE favorite_id = ? AND artwork_id IN ({placeholders})",
                [favorite_id] + data.artwork_ids,
            )
    return {"status": "ok", "removed": len(data.artwork_ids)}


@app.get("/api/authors")
def api_authors():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT au.*, COUNT(a.id) AS artwork_count
               FROM authors au
               LEFT JOIN artworks a ON a.author_id = au.id
               GROUP BY au.id
               ORDER BY artwork_count DESC
               LIMIT 200"""
        ).fetchall()
        return [dict(row) for row in rows]


@app.get("/api/authors/works")
def api_authors_works(
    limit: int = Query(20, ge=1, le=100),
    q: str = Query(""),
    r18: str = Query(""),
):
    with get_db() as conn:
        q = q.strip()
        where = ""
        params = []
        if q:
            where = " WHERE au.name LIKE ?"
            params.append(f"%{q}%")
        authors = conn.execute(
            f"""SELECT au.*, COUNT(a.id) AS artwork_count
               FROM authors au
               LEFT JOIN artworks a ON a.author_id = au.id
               {where}
               GROUP BY au.id
               ORDER BY artwork_count DESC
               LIMIT 200""", params
        ).fetchall()

        result = []
        for au in authors:
            works_where = "a.author_id = ?"
            _r18c = _r18_where("a", r18)
            if _r18c:
                works_where += " AND " + _r18c
            works = conn.execute(
                f"""SELECT a.id, a.pixiv_id, a.title, a.page_count, i.path AS thumb_path
                   FROM artworks a
                   {_cover_image_join()}
                   WHERE {works_where}
                   ORDER BY a.create_date DESC, a.id DESC
                   LIMIT ?""",
                (au["id"], limit),
            ).fetchall()
            result.append({
                "id": au["id"],
                "pixiv_user_id": au["pixiv_user_id"],
                "name": au["name"],
                "artwork_count": au["artwork_count"],
                "works": [dict(w) for w in works],
            })
        return result


@app.get("/api/authors/{author_id}/avatar")
def api_author_avatar(author_id: int):
    """Author avatar: serve from local cache, fetch via direct connect on miss."""
    avatars_dir = os.path.join(paths.DATA_DIR, "metadata", "avatars")
    cache_path = os.path.join(avatars_dir, f"{author_id}.jpg")
    if os.path.exists(cache_path):
        return FileResponse(cache_path, media_type="image/jpeg")

    with get_db() as conn:
        row = conn.execute(
            "SELECT profile_image FROM authors WHERE id = ?", (author_id,)
        ).fetchone()
    if not row or not row["profile_image"]:
        return JSONResponse({"error": "no avatar"}, status_code=404)

    data = fetch_profile_image(row["profile_image"])
    if not data:
        return JSONResponse({"error": "fetch failed"}, status_code=404)
    os.makedirs(avatars_dir, exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(data)
    return FileResponse(cache_path, media_type="image/jpeg")


@app.post("/api/open-folder")
async def api_open_folder(request: Request):
    from pydantic import BaseModel

    class OpenFolder(BaseModel):
        image_id: int

    body = await request.json()
    data = OpenFolder(**body)
    with get_db() as conn:
        row = conn.execute(
            "SELECT path FROM images WHERE id = ?", (data.image_id,)
        ).fetchone()
    if not row:
        return JSONResponse({"error": "Not found"}, status_code=404)
    path = row["path"]
    if not path or not os.path.isabs(path) or not os.path.exists(path):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    try:
        # 用资源管理器打开并选中文件
        subprocess.Popen(["explorer", "/select,", path])
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _error_response(code, message, hint=None, detail=None, status_code=400):
    return JSONResponse(
        {"error": {"code": code, "message": message, "hint": hint, "detail": detail}},
        status_code=status_code,
    )


def _busy_error():
    return _error_response(
        "BUSY", "已有任务进行中",
        "请等待当前任务完成，或在进度窗中点击关闭以停止",
    )


def _pause_error():
    return _error_response(
        "BUSY", "当前任务已暂停",
        "请先继续当前任务，或停止后再发起新任务",
    )


@app.get("/api/scan")
def api_scan():
    if jobs.is_busy():
        return _busy_error()

    source_dirs = _get_source_dirs()
    if not source_dirs:
        return _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                               "请到 设置 → 本地图片目录 填写后重试")
    missing = [d for d in source_dirs if not os.path.isdir(d)]
    if missing:
        return _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{missing[0]}",
                               "请检查设置中的目录路径是否正确")
    log.info("manual scan requested: mode=incremental source_dirs=%s", source_dirs)

    def run_scan(job):
        _run_ingest_pipeline(job, source_dirs, incremental=True)

    job_id, error = jobs.start("scan", run_scan)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "scan"}


@app.get("/api/scan/full")
def api_scan_full():
    if jobs.is_busy():
        return _busy_error()

    source_dirs = _get_source_dirs()
    if not source_dirs:
        return _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                               "请到 设置 → 本地图片目录 填写后重试")
    missing = [d for d in source_dirs if not os.path.isdir(d)]
    if missing:
        return _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{missing[0]}",
                               "请检查设置中的目录路径是否正确")
    log.info("manual scan requested: mode=full source_dirs=%s", source_dirs)

    def run_scan(job):
        _run_ingest_pipeline(job, source_dirs, incremental=False)

    job_id, error = jobs.start("scan_full", run_scan)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "scan_full"}


# ===== 收藏订阅（订阅某用户的公开收藏列表）=====

def _bookmark_source_dir_check():
    """收藏订阅任务共用前置校验，返回 (source_dir, error_response)。"""
    if jobs.is_busy():
        return None, _busy_error()
    source_dir = _primary_source_dir()
    if not source_dir:
        return None, _error_response("NO_SOURCE_DIR", "未设置本地图片目录",
                                     "请到 设置 → 本地图片目录 填写后重试")
    if not os.path.isdir(source_dir):
        return None, _error_response("SOURCE_DIR_NOT_FOUND", f"图片目录不存在：{source_dir}",
                                     "请检查设置中的目录路径是否正确")
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return None, _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                                     "请到 设置 → Pixiv Refresh Token 填写后重试")
    return source_dir, None


def _run_bookmark_check(job, subs, source_dir):
    """收藏订阅检查核心：逐订阅增量下载新收藏 → 有新文件则扫描入库+缩略图+元数据同步。

    subs: [(pixiv_user_id, last_pid)]。与扫描/同步共用 jobs 单任务锁。auth 错误向上抛，
    由 jobs 置为 error。落盘目录按每条收藏自身画师分组（download_single_illust 处理）。
    """
    from datetime import datetime
    from app.pixiv import _build_session_with_referer

    delay = _parse_sync_delay(os.getenv("SYNC_DELAY_MS", ""))
    client = get_pixiv_client()
    client._ensure_auth()
    session = _build_session_with_referer(
        "https://www.pixiv.net/", use_proxy=(client.mode == "proxy")
    )

    total_downloaded = 0
    per_sub = []
    log.info("bookmark check started: subs=%d source_dir=%s", len(subs), source_dir)
    for i, (uid, last_pid) in enumerate(subs):
        if job.cancel_event.is_set():
            break
        job.update("check", i + 1, len(subs), f"检查收藏订阅 {i + 1}/{len(subs)}…")
        r = check_bookmark_subscription(
            client, session, uid, last_pid, source_dir,
            delay_ms=delay, progress_callback=job.update,
            cancel_event=job.cancel_event,
        )
        per_sub.append(r)
        total_downloaded += r["downloaded"]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        brief = json.dumps(
            {"new": r["new_found"], "downloaded": r["downloaded"],
             "failed": r["failed"]}, ensure_ascii=False)
        with get_db() as conn:
            conn.execute(
                "UPDATE bookmark_subs SET last_pid = ?, last_checked = ?, last_result = ? "
                "WHERE pixiv_user_id = ?",
                (r["max_pid_seen"], now_str, brief, uid),
            )

    result = {
        "checked": len(per_sub),
        "downloaded": total_downloaded,
        "new_found": sum(s["new_found"] for s in per_sub),
        "failed": sum(s["failed"] for s in per_sub),
        "cancelled": job.cancel_event.is_set(),
        "subs": [
            {"pixiv_user_id": s["pixiv_user_id"], "new": s["new_found"],
             "downloaded": s["downloaded"], "failed": s["failed"]}
            for s in per_sub if s["new_found"]
        ],
    }
    log.info("bookmark check finished: downloaded=%s new_found=%s failed=%s cancelled=%s",
             total_downloaded, result["new_found"], result["failed"], result["cancelled"])
    # 有新文件才走入库三件套（scan 幂等；后续由并行流水线同步缩略图和元数据）
    if total_downloaded > 0 and not job.cancel_event.is_set():
        job.update("scan", 0, None, "下载完成，正在扫描入库…")
        ingest_result = _run_ingest_pipeline(job, [source_dir], incremental=True)
        result["scan"] = {
            "new_artworks": ingest_result.get("new_artworks", 0),
            "new_images": ingest_result.get("new_images", 0),
        }
        result["thumbnails"] = ingest_result.get("thumbnails", result["thumbnails"])
        result["synced"] = ingest_result.get("synced", 0)
        result["ai_detected"] = ingest_result.get("ai_detected", 0)
        result["sync_failed"] = ingest_result.get("sync_failed", 0)
        result["sync_deleted"] = ingest_result.get("sync_deleted", 0)
        if ingest_result.get("sync_skipped"):
            result["sync_skipped"] = ingest_result["sync_skipped"]
    job.state["result"] = result


@app.get("/api/bookmark-subs")
def api_list_bookmark_subs():
    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.*, a.id AS author_id
               FROM bookmark_subs s
               LEFT JOIN authors a ON a.pixiv_user_id = s.pixiv_user_id
               ORDER BY s.created_at DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/bookmark-subs")
async def api_add_bookmark_sub(request: Request):
    body = await request.json()
    uid = str(body.get("user_id", "")).strip()
    if not uid.isdigit() or int(uid) <= 0:
        return _error_response("INVALID_UID", "用户 ID 无效",
                               "请输入 Pixiv 用户主页 URL 中的数字 ID")
    uid = int(uid)
    name = (body.get("name") or "").strip()
    if not name:
        name = get_pixiv_client().get_user_display_name(uid)
    with get_db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bookmark_subs (pixiv_user_id, name) VALUES (?, ?)",
            (uid, name),
        )
        exists = cur.rowcount == 0
    return {"status": "ok", "exists": exists, "name": name}


def _get_bookmark_sub_or_none(pixiv_user_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM bookmark_subs WHERE pixiv_user_id = ?", (pixiv_user_id,)
        ).fetchone()


@app.post("/api/bookmark-subs/{pixiv_user_id}/check")
def api_check_one_bookmark_sub(pixiv_user_id: int):
    source_dir, err = _bookmark_source_dir_check()
    if err:
        return err
    row = _get_bookmark_sub_or_none(pixiv_user_id)
    if not row:
        return _error_response("NOT_FOUND", "未订阅该用户的收藏", "请先添加订阅")

    def run(job):
        _run_bookmark_check(job, [(pixiv_user_id, row["last_pid"])], source_dir)

    job_id, error = jobs.start("bookmark_check", run)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "bookmark_check"}


@app.get("/api/bookmark-subs/preview/{pixiv_user_id}")
def api_preview_bookmark_sub(pixiv_user_id: int):
    """订阅前预览：该用户公开收藏数与首屏新增数量估算（不占任务锁）。"""
    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                               "请到 设置 → Pixiv Refresh Token 填写后重试")
    client = get_pixiv_client()
    try:
        client._ensure_auth()
        name = client.get_user_display_name(pixiv_user_id)
        # 首屏：不带游标取一页，统计非空条数
        illusts, _ = client.list_user_bookmarks(pixiv_user_id)
        sample = len([i for i in illusts if i.get("id")])
    except Exception as e:
        return _error_response("PREVIEW_FAILED", f"预览失败：{str(e)[:150]}",
                               "请确认用户 ID 正确且其收藏为公开")
    return {"name": name, "first_page": sample}


@app.post("/api/bookmark-subs/{pixiv_user_id}/toggle")
def api_toggle_bookmark_sub(pixiv_user_id: int):
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE bookmark_subs SET auto_download = 1 - auto_download "
            "WHERE pixiv_user_id = ?",
            (pixiv_user_id,),
        )
        if not cur.rowcount:
            return _error_response("NOT_FOUND", "未订阅该用户的收藏", "")
        row = conn.execute(
            "SELECT auto_download FROM bookmark_subs WHERE pixiv_user_id = ?",
            (pixiv_user_id,),
        ).fetchone()
    return {"status": "ok", "auto_download": bool(row["auto_download"])}


@app.delete("/api/bookmark-subs/{pixiv_user_id}")
def api_delete_bookmark_sub(pixiv_user_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM bookmark_subs WHERE pixiv_user_id = ?",
                     (pixiv_user_id,))
    return {"status": "ok"}


@app.get("/api/bookmark-subs/check")
def api_check_all_bookmark_subs():
    """一键检查全部启用中的收藏订阅并自动下载新收藏（完成后入库+缩略图+同步元数据）。"""
    job_id, error = _launch_bookmark_check_all()
    if error:
        return error
    return {"job_id": job_id, "kind": "bookmark_check"}


def _launch_bookmark_check_all():
    source_dir, err = _bookmark_source_dir_check()
    if err:
        return None, err
    with get_db() as conn:
        rows = conn.execute(
            "SELECT pixiv_user_id, last_pid FROM bookmark_subs WHERE auto_download = 1"
        ).fetchall()
    if not rows:
        return None, _error_response("NO_SUBS", "还没有订阅任何收藏列表",
                                      "在「收藏订阅」视图中添加用户 ID")
    subs = [(r["pixiv_user_id"], r["last_pid"]) for r in rows]

    def run(job):
        _run_bookmark_check(job, subs, source_dir)

    job_id, error = jobs.start("bookmark_check", run)
    if error:
        return None, _busy_error()
    return job_id, None


@app.get("/api/sync")
def api_sync(pixiv_id: int = Query(0)):
    if jobs.is_busy():
        return _busy_error()

    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                               "请到 设置 → Pixiv Refresh Token 填写后重试")
    log.info("manual sync requested: mode=incremental pixiv_id=%s", pixiv_id or "all")

    def run_sync(job):
        job.update(phase="sync", message="开始同步…")
        try:
            batch_size = max(1, min(int(os.getenv("PA_SYNC_BATCH_SIZE", "8")), 32))
        except (TypeError, ValueError):
            batch_size = 8
        result = sync_metadata(
            specific_pixiv_id=pixiv_id if pixiv_id else None,
            progress_callback=job.update,
            cancel_event=job.cancel_event,
            commit_each=False,
            commit_batch_size=batch_size,
            batch_callback=lambda batch: publish(
                "metadata_batch_done",
                {"job_id": job.job_id, **batch},
            ),
        )
        job.state["result"] = result

    job_id, error = jobs.start("sync", run_sync)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "sync"}


@app.get("/api/sync/full")
def api_sync_full():
    if jobs.is_busy():
        return _busy_error()

    if not os.getenv("PIXIV_REFRESH_TOKEN", ""):
        return _error_response("NO_TOKEN", "未设置 Pixiv Refresh Token",
                               "请到 设置 → Pixiv Refresh Token 填写后重试")
    log.info("manual metadata rebuild requested: mode=full")

    def run_sync(job):
        job.update(phase="sync", message="开始重建元数据…")
        try:
            batch_size = max(1, min(int(os.getenv("PA_SYNC_BATCH_SIZE", "8")), 32))
        except (TypeError, ValueError):
            batch_size = 8
        result = sync_metadata(
            progress_callback=job.update,
            cancel_event=job.cancel_event,
            force_all=True,
            commit_each=False,
            commit_batch_size=batch_size,
            batch_callback=lambda batch: publish(
                "metadata_batch_done",
                {"job_id": job.job_id, **batch},
            ),
        )
        job.state["result"] = result

    job_id, error = jobs.start("sync_full", run_sync)
    if error:
        return _busy_error()
    return {"job_id": job_id, "kind": "sync_full"}


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    snap = jobs.get(job_id)
    if not snap:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    return snap


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str):
    snap = jobs.cancel(job_id)
    if not snap:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    return snap


@app.post("/api/jobs/{job_id}/pause")
def api_job_pause(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    job.pause()
    return job.snapshot()


@app.post("/api/jobs/{job_id}/resume")
def api_job_resume(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        return _error_response("JOB_NOT_FOUND", "任务不存在或已过期", None, None, 404)
    job.resume()
    return job.snapshot()


@app.get("/api/watch/status")
def api_watch_status():
    return {
        "enabled": _auto_watch_enabled(),
        "configured_dirs": _get_source_dirs(),
        "watcher": _folder_watcher.status(),
        "busy": jobs.is_busy(),
        "last_auto_job_id": _last_auto_job_id,
        "last_auto_job": jobs.get(_last_auto_job_id) if _last_auto_job_id else None,
    }


@app.get("/api/events")
def api_events():
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _read_env_file():
    env_path = paths.ENV_FILE
    result = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    result[key.strip()] = value.strip()
    for key in _SETTING_ENV_KEYS:
        if key not in result and key in os.environ:
            result[key] = os.environ.get(key, "")
    return result


def _write_env_file(settings):
    env_path = paths.ENV_FILE
    existing = _read_env_file()
    existing.update(settings)
    with open(env_path, "w", encoding="utf-8") as f:
        for key, value in existing.items():
            f.write(f"{key}={value}\n")


@app.get("/api/settings")
def api_get_settings():
    settings = _read_env_file()
    token = settings.get("PIXIV_REFRESH_TOKEN", "")
    port_str = (settings.get("PA_PORT", "") or "").strip()
    access_token_auto = LAN_MODE and not (settings.get("PA_ACCESS_TOKEN", "") or "").strip()
    source_dirs = _split_source_dirs(settings.get("IMAGE_SOURCE_DIRS", ""))
    if not source_dirs and settings.get("IMAGE_SOURCE_DIR", ""):
        source_dirs = [settings.get("IMAGE_SOURCE_DIR", "")]
    watcher_status = _folder_watcher.status()
    return {
        "has_token": bool(token),
        "token_preview": token[:8] + "..." if len(token) > 8 else token,
        "image_source_dir": source_dirs[0] if source_dirs else "",
        "image_source_dirs": source_dirs,
        "proxy": settings.get("PIXIV_PROXY", ""),
        "connection_mode": settings.get("PIXIV_MODE", "auto"),
        "image_mirror": settings.get("PIXIV_IMAGE_MIRROR", ""),
        "server_port": int(port_str) if port_str.isdigit() else DEFAULT_PORT,
        "access_token": ACCESS_TOKEN,
        "access_token_auto": access_token_auto,
        "sync_delay_ms": _parse_sync_delay(settings.get("SYNC_DELAY_MS", "")),
        "auto_watch_enabled": (settings.get("AUTO_WATCH_ENABLED", "") or "").strip() == "1",
        "sse_release_delay_seconds": _parse_sse_release_delay(settings.get("PA_SSE_RELEASE_DELAY_SECONDS", ""), 60),
        "show_folder_button": _bool_env(settings.get("PA_SHOW_FOLDER_BTN", ""), True),
        "viewer_auto_original": _bool_env(settings.get("PA_VIEWER_AUTO_ORIGINAL", ""), True),
        "sidebar_hover_expand": _bool_env(settings.get("PA_SIDEBAR_HOVER_EXPAND", ""), True),
        "bookmark_auto_check_enabled": _bool_env(settings.get("BOOKMARK_AUTO_CHECK_ENABLED", ""), False),
        "bookmark_auto_check_interval_minutes": _parse_interval_minutes(settings.get("BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES", ""), 60),
        "bookmark_auto_check_weekday": _parse_weekday(settings.get("BOOKMARK_AUTO_CHECK_WEEKDAY", ""), 0),
        "bookmark_auto_check_time": _parse_hhmm(settings.get("BOOKMARK_AUTO_CHECK_TIME", ""), "03:30"),
        "auto_fail_pause_enabled": _bool_env(settings.get("AUTO_FAIL_PAUSE_ENABLED", ""), True),
        "auto_fail_pause_threshold": _parse_fail_pause_threshold(settings.get("AUTO_FAIL_PAUSE_THRESHOLD", ""), _FAIL_PAUSE_DEFAULT),
        "organize_source_dirs": _split_source_dirs(settings.get("ORGANIZE_SOURCE_DIRS", "")) or source_dirs,
        "organize_output_dir": settings.get("ORGANIZE_OUTPUT_DIR", ""),
        "organize_mode": settings.get("ORGANIZE_MODE", "symlink"),
        "organize_path_rule": settings.get("ORGANIZE_PATH_RULE", "bucket"),
        "organize_path_template": settings.get("ORGANIZE_PATH_TEMPLATE", ""),
        "organize_unknown_as_human": _bool_env(settings.get("ORGANIZE_UNKNOWN_AS_HUMAN", ""), False),
        "organize_rename_enabled": _bool_env(settings.get("ORGANIZE_RENAME_ENABLED", ""), False),
        "organize_rename_rule": settings.get("ORGANIZE_RENAME_RULE", "title_author_page"),
        "organize_rename_template": settings.get("ORGANIZE_RENAME_TEMPLATE", ""),
        "auto_watch_running": watcher_status["running"],
        "auto_watch_available": watcher_status["available"],
    }


@app.post("/api/settings")
async def api_update_settings(request: Request):
    from pydantic import BaseModel

    class SettingsUpdate(BaseModel):
        refresh_token: str = ""
        image_source_dir: str = ""
        image_source_dirs: list[str] = []
        proxy: str = ""
        connection_mode: str = ""
        server_port: str = ""
        access_token: str = ""
        sync_delay_ms: str = ""
        image_mirror: str = ""
        auto_watch_enabled: bool = False
        sse_release_delay_seconds: str = ""
        show_folder_button: bool = True
        viewer_auto_original: bool = True
        sidebar_hover_expand: bool = True
        bookmark_auto_check_enabled: bool = False
        bookmark_auto_check_interval_minutes: str = ""
        bookmark_auto_check_weekday: str = ""
        bookmark_auto_check_time: str = ""
        auto_fail_pause_enabled: bool = True
        auto_fail_pause_threshold: str = ""
        organize_source_dirs: list[str] = []
        organize_output_dir: str = ""
        organize_mode: str = ""
        organize_path_rule: str = ""
        organize_path_template: str = ""
        organize_unknown_as_human: bool = False
        organize_rename_enabled: bool = False
        organize_rename_rule: str = ""
        organize_rename_template: str = ""

    body = await request.json()
    data = SettingsUpdate(**body)
    updates = {}
    if data.refresh_token:
        updates["PIXIV_REFRESH_TOKEN"] = data.refresh_token
        os.environ["PIXIV_REFRESH_TOKEN"] = data.refresh_token
    if "image_source_dirs" in body or "image_source_dir" in body:
        source_dirs = data.image_source_dirs if "image_source_dirs" in body else [data.image_source_dir]
        source_dirs = [d.strip() for d in source_dirs if (d or "").strip()]
        joined_dirs = SOURCE_DIR_SEPARATOR.join(source_dirs)
        primary_dir = source_dirs[0] if source_dirs else ""
        updates["IMAGE_SOURCE_DIR"] = primary_dir
        updates["IMAGE_SOURCE_DIRS"] = joined_dirs
        os.environ["IMAGE_SOURCE_DIR"] = primary_dir
        os.environ["IMAGE_SOURCE_DIRS"] = joined_dirs
    if "proxy" in body:
        updates["PIXIV_PROXY"] = data.proxy
        os.environ["PIXIV_PROXY"] = data.proxy
        reset_pixiv_client()
    if "connection_mode" in body and data.connection_mode in ("direct", "proxy", "auto"):
        updates["PIXIV_MODE"] = data.connection_mode
        os.environ["PIXIV_MODE"] = data.connection_mode
        reset_pixiv_client()
    if "server_port" in body:
        port_str = (data.server_port or "").strip()
        if port_str:
            if port_str.isdigit() and 1 <= int(port_str) <= 65535:
                updates["PA_PORT"] = port_str
                os.environ["PA_PORT"] = port_str
            else:
                return _error_response("INVALID_PORT", "端口需为 1-65535 的整数",
                                       "请检查端口填写是否正确")
    if "access_token" in body:
        token_str = (data.access_token or "").strip()
        updates["PA_ACCESS_TOKEN"] = token_str
        os.environ["PA_ACCESS_TOKEN"] = token_str
        # 立即生效：留空 + 局域网模式则自动生成新令牌
        if token_str:
            globals()["ACCESS_TOKEN"] = token_str
        elif LAN_MODE:
            globals()["ACCESS_TOKEN"] = secrets.token_urlsafe(12)
        else:
            globals()["ACCESS_TOKEN"] = ""
    if "sync_delay_ms" in body:
        delay_str = (data.sync_delay_ms or "").strip()
        if delay_str:
            if delay_str.isdigit() and 0 <= int(delay_str) <= _SYNC_DELAY_MAX:
                updates["SYNC_DELAY_MS"] = str(int(delay_str))
                os.environ["SYNC_DELAY_MS"] = str(int(delay_str))
            else:
                return _error_response("INVALID_DELAY", "同步间隔需为 0-10000 的整数（毫秒）",
                                       "请检查填写是否正确，0 表示不限速")
    if "image_mirror" in body:
        mirror = (data.image_mirror or "").strip()
        if mirror:
            mirror = re.sub(r"^https?://", "", mirror).strip().rstrip("/")
            if re.search(r"\s", mirror) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9.-]*(/[^\s]*)?$", mirror):
                return _error_response("INVALID_MIRROR", "镜像域名无效",
                                       "请填写形如 i.pixiv.re 的域名（可带路径前缀），不要包含空格")
        updates["PIXIV_IMAGE_MIRROR"] = mirror
        os.environ["PIXIV_IMAGE_MIRROR"] = mirror
    if "auto_watch_enabled" in body:
        auto_watch = "1" if data.auto_watch_enabled else "0"
        updates["AUTO_WATCH_ENABLED"] = auto_watch
        os.environ["AUTO_WATCH_ENABLED"] = auto_watch
    if "sse_release_delay_seconds" in body:
        raw_delay = (data.sse_release_delay_seconds or "").strip()
        if raw_delay.isdigit() and 10 <= int(raw_delay) <= 86400:
            updates["PA_SSE_RELEASE_DELAY_SECONDS"] = str(int(raw_delay))
            os.environ["PA_SSE_RELEASE_DELAY_SECONDS"] = str(int(raw_delay))
        else:
            return _error_response("INVALID_SSE_DELAY", "SSE 断连释放时间需为 10-86400 秒", "请填写有效的秒数")
    if "show_folder_button" in body:
        show_folder = "1" if data.show_folder_button else "0"
        updates["PA_SHOW_FOLDER_BTN"] = show_folder
        os.environ["PA_SHOW_FOLDER_BTN"] = show_folder
    if "viewer_auto_original" in body:
        viewer_original = "1" if data.viewer_auto_original else "0"
        updates["PA_VIEWER_AUTO_ORIGINAL"] = viewer_original
        os.environ["PA_VIEWER_AUTO_ORIGINAL"] = viewer_original
    if "sidebar_hover_expand" in body:
        hover_expand = "1" if data.sidebar_hover_expand else "0"
        updates["PA_SIDEBAR_HOVER_EXPAND"] = hover_expand
        os.environ["PA_SIDEBAR_HOVER_EXPAND"] = hover_expand
    if "bookmark_auto_check_enabled" in body:
        auto_check = "1" if data.bookmark_auto_check_enabled else "0"
        updates["BOOKMARK_AUTO_CHECK_ENABLED"] = auto_check
        os.environ["BOOKMARK_AUTO_CHECK_ENABLED"] = auto_check
    if "bookmark_auto_check_interval_minutes" in body:
        interval = (data.bookmark_auto_check_interval_minutes or "").strip()
        if interval:
            if interval.isdigit() and 5 <= int(interval) <= 1440:
                updates["BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES"] = str(int(interval))
                os.environ["BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES"] = str(int(interval))
            else:
                return _error_response("INVALID_INTERVAL", "定时间隔需为 5-1440 的整数（分钟）",
                                       "请检查填写是否正确")
        else:
            updates["BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES"] = "60"
            os.environ["BOOKMARK_AUTO_CHECK_INTERVAL_MINUTES"] = "60"
    if "bookmark_auto_check_weekday" in body:
        weekday = (data.bookmark_auto_check_weekday or "").strip()
        if weekday and weekday.isdigit() and 0 <= int(weekday) <= 6:
            updates["BOOKMARK_AUTO_CHECK_WEEKDAY"] = str(int(weekday))
            os.environ["BOOKMARK_AUTO_CHECK_WEEKDAY"] = str(int(weekday))
        else:
            updates["BOOKMARK_AUTO_CHECK_WEEKDAY"] = "0"
            os.environ["BOOKMARK_AUTO_CHECK_WEEKDAY"] = "0"
    if "bookmark_auto_check_time" in body:
        raw_time = (data.bookmark_auto_check_time or "").strip()
        parsed_time = _parse_hhmm(raw_time, "")
        if parsed_time:
            updates["BOOKMARK_AUTO_CHECK_TIME"] = parsed_time
            os.environ["BOOKMARK_AUTO_CHECK_TIME"] = parsed_time
        else:
            return _error_response("INVALID_SCHEDULE_TIME", "定时时间需为 HH:MM 格式",
                                   "请填写 00:00 到 23:59 之间的北京时间")
    if "auto_fail_pause_enabled" in body:
        fail_pause = "1" if data.auto_fail_pause_enabled else "0"
        updates["AUTO_FAIL_PAUSE_ENABLED"] = fail_pause
        os.environ["AUTO_FAIL_PAUSE_ENABLED"] = fail_pause
    if "auto_fail_pause_threshold" in body:
        threshold = (data.auto_fail_pause_threshold or "").strip()
        if threshold:
            if threshold.isdigit() and 2 <= int(threshold) <= _FAIL_PAUSE_MAX:
                updates["AUTO_FAIL_PAUSE_THRESHOLD"] = str(int(threshold))
                os.environ["AUTO_FAIL_PAUSE_THRESHOLD"] = str(int(threshold))
            else:
                return _error_response("INVALID_THRESHOLD", f"自动暂停次数需为 2-{_FAIL_PAUSE_MAX} 的整数",
                                       "请检查填写是否正确")
        else:
            updates["AUTO_FAIL_PAUSE_THRESHOLD"] = str(_FAIL_PAUSE_DEFAULT)
            os.environ["AUTO_FAIL_PAUSE_THRESHOLD"] = str(_FAIL_PAUSE_DEFAULT)
    if any(key in body for key in (
        "organize_source_dirs", "organize_output_dir", "organize_mode",
        "organize_path_rule", "organize_path_template", "organize_unknown_as_human",
        "organize_rename_enabled", "organize_rename_rule", "organize_rename_template",
    )):
        organize_dirs = [d.strip() for d in data.organize_source_dirs if (d or "").strip()]
        organize_mode = data.organize_mode or "symlink"
        organize_rule = data.organize_path_rule or "bucket"
        rename_rule = data.organize_rename_rule or "title_author_page"
        if organize_mode not in ("copy", "move", "symlink", "hardlink"):
            return _error_response("INVALID_ORGANIZE_MODE", "整理方式无效", "请选择复制、移动、软链接或硬链接")
        if organize_rule not in PATH_RULES:
            return _error_response("INVALID_ORGANIZE_RULE", "整理路径规则无效", "请选择一个预设路径规则")
        if rename_rule not in RENAME_RULES:
            return _error_response("INVALID_RENAME_RULE", "重命名规则无效", "请选择一个预设重命名规则")
        organize_updates = {
            "ORGANIZE_SOURCE_DIRS": SOURCE_DIR_SEPARATOR.join(organize_dirs),
            "ORGANIZE_OUTPUT_DIR": (data.organize_output_dir or "").strip(),
            "ORGANIZE_MODE": organize_mode,
            "ORGANIZE_PATH_RULE": organize_rule,
            "ORGANIZE_PATH_TEMPLATE": (data.organize_path_template or "").replace("\r", "").replace("\n", "").strip(),
            "ORGANIZE_UNKNOWN_AS_HUMAN": "1" if data.organize_unknown_as_human else "0",
            "ORGANIZE_RENAME_ENABLED": "1" if data.organize_rename_enabled else "0",
            "ORGANIZE_RENAME_RULE": rename_rule,
            "ORGANIZE_RENAME_TEMPLATE": (data.organize_rename_template or "").replace("\r", "").replace("\n", "").strip(),
        }
        updates.update(organize_updates)
        os.environ.update(organize_updates)
    _write_env_file(updates)
    watcher_result = _restart_folder_watcher()
    _restart_bookmark_scheduler()
    return {"status": "ok", "watcher": watcher_result}


@app.post("/api/settings/refresh-ips")
def api_refresh_ips():
    from app.pixiv import refresh_direct_ips
    updated = refresh_direct_ips()
    return {"status": "ok", "updated": updated}


@app.get("/image/{filepath:path}")
def serve_image(filepath: str):
    decoded = unquote(filepath)
    # 只允许读取已登记在库的作品图片，防止局域网模式下被利用读取任意本地文件
    if not _is_registered_image(decoded):
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(decoded)


@app.get("/image-thumb/{image_id}")
def serve_image_thumbnail(image_id: int):
    """Serve a cached thumbnail for a database-registered image only."""
    with get_db() as conn:
        row = conn.execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row or not row["path"] or not os.path.isfile(row["path"]):
        return JSONResponse({"error": "Not found"}, status_code=404)
    thumb_path = generate_image_thumbnail(row["path"])
    if not thumb_path or not os.path.isfile(thumb_path):
        return JSONResponse({"error": "Thumbnail unavailable"}, status_code=404)
    return FileResponse(thumb_path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})

def _is_registered_image(path):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT 1 FROM images WHERE path = ?", (path,)).fetchone()
            return row is not None
    except Exception:
        return False


def _restore_duplicate_filename(conn, artwork_id):
    """Remove a generated Windows-style suffix when its base name is free."""
    rows = conn.execute(
        "SELECT id, path FROM images WHERE artwork_id = ?",
        (artwork_id,),
    ).fetchall()
    pattern = re.compile(r"^(.*) \((\d+)\)(\.[^.]+)$")
    for row in rows:
        path = row["path"]
        directory, filename = os.path.split(path)
        match = pattern.match(filename)
        if not match:
            continue
        normal_path = os.path.join(directory, match.group(1) + match.group(3))
        if os.path.normcase(normal_path) == os.path.normcase(path):
            continue
        if os.path.lexists(normal_path):
            continue
        try:
            os.rename(path, normal_path)
        except OSError as exc:
            log.warning("duplicate survivor rename failed: %s -> %s error=%s", path, normal_path, exc)
            continue
        conn.execute(
            "UPDATE images SET path = ?, file_name = ? WHERE id = ?",
            (normal_path, os.path.basename(normal_path).lower(), row["id"]),
        )
        log.info("duplicate survivor renamed: %s -> %s", path, normal_path)


@app.get("/api/duplicates")
def api_duplicates():
    with get_db() as conn:
        groups = conn.execute(
            """SELECT group_key, file_name, file_size, sha256, updated_at
               FROM duplicate_groups
               ORDER BY updated_at DESC, file_name ASC
               LIMIT 200"""
        ).fetchall()
        result = []
        for group in groups:
            rows = conn.execute(
                """SELECT di.checked, i.id AS image_id, i.path, i.page,
                          a.id AS artwork_id, a.pixiv_id, a.title
                   FROM duplicate_images di
                   JOIN images i ON i.id = di.image_id
                   JOIN artworks a ON a.id = i.artwork_id
                   WHERE di.group_key = ?
                   ORDER BY i.id ASC""",
                (group["group_key"],),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    stat = os.stat(item["path"])
                    item["created_at"] = getattr(stat, "st_birthtime", None) or getattr(stat, "st_ctime", None) or 0
                except OSError:
                    item["created_at"] = 0
                items.append(item)
            result.append({
                "group_key": group["group_key"],
                "file_name": group["file_name"],
                "file_size": group["file_size"],
                "updated_at": group["updated_at"],
                "items": items,
            })
        suspects = conn.execute(
            """SELECT sg.group_key, sg.artwork_id, sg.page, sg.updated_at,
                      a.pixiv_id, a.title
               FROM suspect_groups sg
               JOIN artworks a ON a.id = sg.artwork_id
               ORDER BY sg.updated_at DESC, sg.artwork_id ASC, sg.page ASC
               LIMIT 200"""
        ).fetchall()
        suspect_result = []
        for group in suspects:
            rows = conn.execute(
                """SELECT si.checked, i.id AS image_id, i.path, i.page,
                          i.width, i.height, i.file_size,
                          a.id AS artwork_id, a.pixiv_id, a.title
                   FROM suspect_images si
                   JOIN images i ON i.id = si.image_id
                   JOIN artworks a ON a.id = i.artwork_id
                   WHERE si.group_key = ?
                   ORDER BY
                       CASE WHEN COALESCE(i.width, 0) * COALESCE(i.height, 0) > 0
                            THEN COALESCE(i.width, 0) * COALESCE(i.height, 0)
                            ELSE COALESCE(i.file_size, 0) END DESC,
                       i.id ASC""",
                (group["group_key"],),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                try:
                    stat = os.stat(item["path"])
                    item["created_at"] = getattr(stat, "st_birthtime", None) or getattr(stat, "st_ctime", None) or 0
                except OSError:
                    item["created_at"] = 0
                items.append(item)
            suspect_result.append({
                "group_key": group["group_key"],
                "file_name": f"{group['title'] or group['pixiv_id']} · P{group['page']}",
                "file_size": None,
                "updated_at": group["updated_at"],
                "items": items,
            })
    return {"groups": result, "suspects": suspect_result, "total": len(result), "suspect_total": len(suspect_result)}


@app.post("/api/duplicates/delete")
async def api_delete_duplicates(request: Request):
    body = await request.json()
    image_ids = [int(x) for x in body.get("image_ids", []) if str(x).isdigit()]
    if not image_ids:
        return _error_response("NO_SELECTION", "未选择要删除的重复图片", "请先勾选需要删除的重复图片")
    deleted = 0
    failed = 0
    errors = []
    touched_artworks = set()
    with get_db() as conn:
        placeholders = ",".join("?" for _ in image_ids)
        rows = conn.execute(
            f"""SELECT i.id, i.artwork_id, i.path
                FROM images i
                LEFT JOIN duplicate_images di ON di.image_id = i.id
                LEFT JOIN suspect_images si ON si.image_id = i.id
                WHERE i.id IN ({placeholders})
                  AND (di.image_id IS NOT NULL OR si.image_id IS NOT NULL)
                GROUP BY i.id, i.artwork_id, i.path""",
            image_ids,
        ).fetchall()
        found_ids = set()
        for row in rows:
            found_ids.add(int(row["id"]))
            try:
                # Do not remove the DB record unless the mounted source file is
                # actually gone. This keeps failed deletes visible after refresh.
                if not os.path.lexists(row["path"]):
                    raise FileNotFoundError("源文件不存在，已保留数据库记录")
                os.remove(row["path"])
            except FileNotFoundError:
                failed += 1
                errors.append({
                    "image_id": row["id"],
                    "path": row["path"],
                    "error": "源文件不存在，已保留数据库记录",
                })
                continue
            except Exception as exc:
                log.warning("duplicate source file could not be removed: path=%s error=%s", row["path"], exc)
                failed += 1
                errors.append({"image_id": row["id"], "path": row["path"], "error": str(exc)})
                continue
            try:
                conn.execute("DELETE FROM duplicate_images WHERE image_id = ?", (row["id"],))
                conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
                touched_artworks.add(row["artwork_id"])
                deleted += 1
            except Exception:
                failed += 1
                log.exception("duplicate database row remove failed: image_id=%s", row["id"])
        missing_ids = set(image_ids) - found_ids
        failed += len(missing_ids)
        if missing_ids:
            log.warning("duplicate delete selected IDs are not active duplicate candidates: %s", sorted(missing_ids))
            errors.extend({
                "image_id": image_id,
                "error": "图片记录不存在或已不属于重复项，未执行删除",
            } for image_id in sorted(missing_ids))
        for artwork_id in touched_artworks:
            _normalize_image_pages(conn, artwork_id)
            _restore_duplicate_filename(conn, artwork_id)
            count = conn.execute("SELECT COUNT(*) FROM images WHERE artwork_id = ?", (artwork_id,)).fetchone()[0]
            cover = conn.execute(
                "SELECT path FROM images WHERE artwork_id = ? ORDER BY page ASC, id ASC LIMIT 1",
                (artwork_id,),
            ).fetchone()
            if count:
                conn.execute(
                    "UPDATE artworks SET page_count = ?, local_path = ? WHERE id = ?",
                    (count, cover["path"], artwork_id),
                )
            else:
                conn.execute("DELETE FROM artworks WHERE id = ?", (artwork_id,))
        refresh_duplicate_candidates(conn)
    log.info("duplicate delete finished: selected=%d deleted=%d failed=%d", len(image_ids), deleted, failed)
    return {"status": "ok", "deleted": deleted, "failed": failed, "errors": errors}


@app.post("/api/organize")
async def api_organize(request: Request):
    if jobs.is_busy():
        return _busy_error()
    body = await request.json()
    raw_source_dirs = body.get("source_dirs")
    if isinstance(raw_source_dirs, list):
        source_dirs = [str(value).strip() for value in raw_source_dirs if str(value).strip()]
    else:
        source_dir = (body.get("source_dir") or "").strip()
        source_dirs = [source_dir] if source_dir else []
    output_dir = (body.get("output_dir") or "").strip()
    mode = (body.get("mode") or "copy").strip()
    path_rule = (body.get("path_rule") or "shaft_classic").strip()
    path_template = (body.get("path_template") or "").replace("\r", "").replace("\n", "").strip()
    unknown_as_human = bool(body.get("unknown_as_human", False))
    rename_enabled = bool(body.get("rename_enabled", False))
    rename_rule = (body.get("rename_rule") or "keep").strip()
    rename_template = (body.get("rename_template") or "").replace("\r", "").replace("\n", "").strip()
    if not source_dirs or not output_dir:
        return _error_response("INVALID_ORGANIZE_DIR", "请填写源文件夹和输出文件夹", "整理文件夹不与扫描目录绑定，需要单独指定")
    if mode not in ("copy", "move", "symlink", "hardlink"):
        return _error_response("INVALID_ORGANIZE_MODE", "整理方式无效", "请选择复制、移动、软链接或硬链接")
    if path_rule not in PATH_RULES:
        return _error_response("INVALID_ORGANIZE_RULE", "整理路径规则无效", "请选择一个预设路径规则")
    if rename_rule not in RENAME_RULES:
        return _error_response("INVALID_RENAME_RULE", "重命名规则无效", "请选择一个预设重命名规则")
    if path_rule == "custom" and not path_template:
        return _error_response("EMPTY_PATH_TEMPLATE", "自定义整理路径不能为空", "请填写路径模板或选择一个预设")
    if rename_enabled and rename_rule == "custom" and not rename_template:
        return _error_response("EMPTY_RENAME_TEMPLATE", "自定义重命名模板不能为空", "请填写文件名模板或选择一个预设")
    organize_settings = {
        "ORGANIZE_SOURCE_DIRS": SOURCE_DIR_SEPARATOR.join(source_dirs),
        "ORGANIZE_OUTPUT_DIR": output_dir,
        "ORGANIZE_MODE": mode,
        "ORGANIZE_PATH_RULE": path_rule,
        "ORGANIZE_PATH_TEMPLATE": path_template,
        "ORGANIZE_UNKNOWN_AS_HUMAN": "1" if unknown_as_human else "0",
        "ORGANIZE_RENAME_ENABLED": "1" if rename_enabled else "0",
        "ORGANIZE_RENAME_RULE": rename_rule,
        "ORGANIZE_RENAME_TEMPLATE": rename_template,
    }
    _write_env_file(organize_settings)
    os.environ.update(organize_settings)

    def run(job):
        result = organize_files(
            source_dirs,
            output_dir,
            mode,
            path_rule=path_rule,
            path_template=path_template,
            progress_callback=job.update,
            cancel_event=job.cancel_event,
            unknown_as_human=unknown_as_human,
            rename_enabled=rename_enabled,
            rename_rule=rename_rule,
            rename_template=rename_template,
        )
        job.state["result"] = result
        log.info(
            "organize job finished: done=%s failed=%s classifications=%s",
            result.get("done", 0),
            result.get("failed", 0),
            result.get("classification_counts", {}),
        )

    job_id, error = jobs.start("organize", run)
    if error:
        return _busy_error()
    log.info(
        "organize job requested: mode=%s path_rule=%s unknown_as_human=%s sources=%s output=%s",
        mode,
        path_rule,
        unknown_as_human,
        source_dirs,
        output_dir,
    )
    return {"job_id": job_id, "kind": "organize"}


@app.post("/api/organize/undo")
def api_organize_undo():
    if jobs.is_busy():
        return _busy_error()

    def run(job):
        result = undo_last_organize(progress_callback=job.update, cancel_event=job.cancel_event)
        job.state["result"] = result

    job_id, error = jobs.start("organize_undo", run)
    if error:
        return _busy_error()
    log.info("organize undo job requested")
    return {"job_id": job_id, "kind": "organize_undo"}


@app.get("/artwork/{artwork_id}", response_class=HTMLResponse)
def artwork_page(request: Request, artwork_id: int):
    return templates.TemplateResponse(
        request, "artwork.html", {"artwork_id": artwork_id}
    )


@app.get("/author/{author_id}", response_class=HTMLResponse)
def author_page(request: Request, author_id: int):
    return templates.TemplateResponse(
        request, "author.html", {"author_id": author_id}
    )


@app.get("/tag/{tag_name:path}", response_class=HTMLResponse)
def tag_page(request: Request, tag_name: str):
    return templates.TemplateResponse(
        request, "tag.html", {"tag_name": tag_name}
    )


if __name__ == "__main__":
    print("Use run.py in the project root to start the server.")
