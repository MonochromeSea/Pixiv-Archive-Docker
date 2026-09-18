import os
import re
import hashlib
import logging
import time
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
from app import paths
from app.database import get_db, init_db

load_dotenv(paths.ENV_FILE)

log = logging.getLogger("pixiv_archive.scanner")

METADATA_DIR = os.getenv("METADATA_DIR", "metadata")
THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "thumbnails")

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
PIXIV_ID_MULTI_PATTERN = re.compile(r"(\d{7,10})\s*[_\-\s]\s*p(\d+)", re.IGNORECASE)
PIXIV_ID_MULTI_NO_UNDERSCORE_PATTERN = re.compile(r"(\d{7,10})p(\d+)", re.IGNORECASE)
PAGE_SUFFIX_PATTERN = re.compile(r"(?:^|[_\-\s])p(\d+)$", re.IGNORECASE)
PIXIV_ID_SEARCH_PATTERN = re.compile(r"\d{7,10}")


def _scan_hash_mode(incremental):
    mode = (os.getenv("PA_SCAN_HASH_MODE", "full") or "full").strip().lower()
    if mode in ("off", "0", "false", "none"):
        return "off"
    if mode in ("always", "all", "1", "true"):
        return "always"
    return "candidates"


def _should_hash_file(incremental):
    return _scan_hash_mode(incremental) == "always"


def _file_name_key(filepath):
    return os.path.basename(filepath).lower()


def extract_pixiv_info(filename):
    match = PIXIV_ID_MULTI_PATTERN.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = PIXIV_ID_MULTI_NO_UNDERSCORE_PATTERN.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    name_no_ext = os.path.splitext(filename)[0]
    matches = PIXIV_ID_SEARCH_PATTERN.findall(name_no_ext)
    if matches:
        return int(matches[-1]), 0
    return None, None


def extract_page_from_filename(filename, fallback=0):
    name_no_ext = os.path.splitext(os.path.basename(filename))[0]
    match = PAGE_SUFFIX_PATTERN.search(name_no_ext)
    if match:
        return int(match.group(1))
    _pixiv_id, page = extract_pixiv_info(filename)
    return page if page is not None else fallback


def compute_sha256(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_file_stat(filepath):
    st = os.stat(filepath)
    return st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))


def _wait_if_paused(cancel_event=None, pause_event=None):
    while pause_event is not None and pause_event.is_set():
        if cancel_event and cancel_event.is_set():
            return False
        time.sleep(0.2)
    return not (cancel_event and cancel_event.is_set())


def _scan_commit_batch():
    raw = (os.getenv("PA_SCAN_COMMIT_BATCH", "8") or "8").strip()
    try:
        return max(1, min(int(raw), 100))
    except ValueError:
        return 8


def scan_directory(source_dir, progress_callback=None, cancel_event=None, pause_event=None, artwork_callback=None, incremental=False, failure_callback=None, initialize_db=True, changed_files=None):
    if initialize_db:
        init_db()

    if not os.path.isdir(source_dir):
        log.warning("scan directory not found: %s", source_dir)
        return {"error": f"Directory not found: {source_dir}"}

    new_artworks = 0
    new_images = 0
    skipped = 0
    duplicates = 0
    changed_duplicate_keys = set()

    image_files = []
    mode = "incremental" if incremental else "full"
    log.info("%s scan started for source directory: %s; hash_mode=%s",
             mode, source_dir, _scan_hash_mode(incremental))
    if changed_files is not None:
        # Watchdog may emit several events for one copied or renamed file.
        seen_changed = set()
        for filepath in changed_files:
            if not _wait_if_paused(cancel_event, pause_event):
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            normalized = os.path.normcase(os.path.abspath(filepath))
            if normalized in seen_changed:
                continue
            seen_changed.add(normalized)
            if not os.path.isfile(filepath):
                continue
            ext = os.path.splitext(filepath)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                image_files.append(filepath)
    else:
        for root, dirs, files in os.walk(source_dir):
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            if not _wait_if_paused(cancel_event, pause_event):
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            for filename in files:
                if not _wait_if_paused(cancel_event, pause_event):
                    return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
                ext = os.path.splitext(filename)[1].lower()
                if ext in IMAGE_EXTENSIONS:
                    image_files.append(os.path.join(root, filename))
                    if progress_callback and len(image_files) % 100 == 0:
                        progress_callback("scan", len(image_files), None,
                                          f"正在扫描目录…已发现 {len(image_files)} 张图片")

    if cancel_event and cancel_event.is_set():
        return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}

    log.info("%s source walk finished: %s; image_files=%d", mode, source_dir, len(image_files))

    grouped = {}
    for filepath in image_files:
        filename = os.path.basename(filepath)
        pixiv_id, page = extract_pixiv_info(filename)
        if pixiv_id is None:
            continue
        if pixiv_id not in grouped:
            grouped[pixiv_id] = []
        grouped[pixiv_id].append((filepath, page))

    log.info("pixiv grouping finished: %s; artworks=%d", source_dir, len(grouped))

    with get_db() as conn:
        if changed_files is not None:
            # 事件扫描只收到变化文件；把同一作品已入库的其他页补进来，
            # 这样页码归一化、封面选择和元数据触发仍保持原有语义。
            changed_ids = set(grouped)
            for pixiv_id in changed_ids:
                row = conn.execute(
                    "SELECT id FROM artworks WHERE pixiv_id = ?",
                    (pixiv_id,),
                ).fetchone()
                if not row:
                    continue
                known = {
                    os.path.normcase(os.path.abspath(path))
                    for path, _ in grouped[pixiv_id]
                }
                old_rows = conn.execute(
                    "SELECT path, page FROM images WHERE artwork_id = ?",
                    (row["id"],),
                ).fetchall()
                for old in old_rows:
                    normalized = os.path.normcase(os.path.abspath(old["path"]))
                    if normalized not in known and os.path.isfile(old["path"]):
                        grouped[pixiv_id].append((old["path"], old["page"]))
            log.info(
                "event incremental grouping finished: changed_files=%d artworks=%d",
                len(image_files),
                len(grouped),
            )
        total_works = len(grouped)
        pending_callbacks = []
        commit_batch = _scan_commit_batch()

        def flush_batch():
            if not pending_callbacks:
                return
            conn.commit()
            callbacks = pending_callbacks[:]
            pending_callbacks.clear()
            if artwork_callback:
                for payload in callbacks:
                    artwork_callback(payload)

        for i, (pixiv_id, images) in enumerate(grouped.items()):
            if cancel_event and cancel_event.is_set():
                flush_batch()
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            if not _wait_if_paused(cancel_event, pause_event):
                flush_batch()
                return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
            if progress_callback:
                progress_callback("import", i + 1, total_works, f"写入数据库…{i + 1}/{total_works}（PID {pixiv_id}）")
            images.sort(key=lambda x: (extract_page_from_filename(x[0], x[1]), x[0].lower()))

            existing = conn.execute(
                "SELECT id, local_path FROM artworks WHERE pixiv_id = ?", (pixiv_id,)
            ).fetchone()

            is_new_artwork = not bool(existing)
            if existing:
                artwork_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO artworks (pixiv_id, page_count, local_path, first_seen) VALUES (?, ?, ?, ?)",
                    (pixiv_id, len(images), images[0][0],
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
                artwork_id = cursor.lastrowid
                new_artworks += 1

            cover_path = images[0][0]
            cover_changed = is_new_artwork or not existing or existing["local_path"] != cover_path
            thumbnail_path = os.path.join(paths.DATA_DIR, THUMBNAIL_DIR, f"{pixiv_id}.jpg")
            thumbnail_missing = not os.path.exists(thumbnail_path)

            conn.execute(
                "UPDATE artworks SET page_count = ?, local_path = ? WHERE id = ?",
                (len(images), cover_path, artwork_id),
            )

            changed_images = 0
            for filepath, page in images:
                if not _wait_if_paused(cancel_event, pause_event):
                    # 当前作品尚未入队，回滚整个未提交批次，避免留下无法继续处理的记录。
                    conn.rollback()
                    pending_callbacks.clear()
                    return {"cancelled": True, "new_artworks": new_artworks, "new_images": new_images}
                try:
                    file_size, file_mtime_ns = _get_file_stat(filepath)
                except Exception:
                    if failure_callback:
                        failure_callback("scan", filepath, "stat")
                    file_size = None
                    file_mtime_ns = None
                existing_img = conn.execute(
                    "SELECT id, file_name, sha256, file_mtime_ns, file_size FROM images WHERE path = ?",
                    (filepath,),
                ).fetchone()
                stat_unchanged = bool(
                    existing_img
                    and existing_img["file_mtime_ns"] == file_mtime_ns
                    and existing_img["file_size"] == file_size
                )
                # Full scans still need to walk the directory so missing
                # files can be pruned, but unchanged files do not need a
                # database write or downstream thumbnail/metadata work.
                if stat_unchanged:
                    skipped += 1
                    continue
                file_name = _file_name_key(filepath)
                changed_duplicate_keys.add((artwork_id, page))
                # 文件 stat 变化后旧 hash 已不可信；仅在文件未变化时复用。
                sha = existing_img["sha256"] if stat_unchanged else None
                if _should_hash_file(incremental):
                    try:
                        sha = compute_sha256(filepath)
                    except Exception:
                        if failure_callback:
                            failure_callback("scan", filepath, "hash")
                        sha = None

                if existing_img:
                    conn.execute(
                        "UPDATE images SET artwork_id = ?, page = ?, file_name = ?, sha256 = ?, file_mtime_ns = ?, file_size = ? WHERE id = ?",
                        (artwork_id, page, file_name, sha, file_mtime_ns, file_size, existing_img["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO images (artwork_id, page, path, file_name, sha256, file_mtime_ns, file_size) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (artwork_id, page, filepath, file_name, sha, file_mtime_ns, file_size),
                    )
                new_images += 1
                changed_images += 1

            _normalize_image_pages(conn, artwork_id)
            pending_callbacks.append({
                "artwork_id": artwork_id,
                "pixiv_id": pixiv_id,
                "cover_path": images[0][0],
                "page_count": len(images),
                "is_new_artwork": is_new_artwork,
                "new_images": changed_images,
                "cover_changed": cover_changed,
                "thumbnail_missing": thumbnail_missing,
                "needs_thumbnail": cover_changed or thumbnail_missing,
                "needs_sync": is_new_artwork or changed_images > 0,
            })
            if len(pending_callbacks) >= commit_batch:
                flush_batch()

        if incremental:
            prune_result = {"pruned_images": 0, "pruned_artworks": 0}
        else:
            prune_result = _prune_missing(conn, source_dir, image_files)
        flush_batch()
        if incremental and new_artworks == 0 and new_images == 0:
            duplicate_result = {"groups": 0, "images": 0, "cancelled": False, "skipped": True}
            log.info("duplicate candidate refresh skipped: incremental scan had no changes")
        elif incremental:
            duplicate_result = refresh_duplicate_candidates(
                conn,
                cancel_event,
                pause_event,
                failure_callback,
                candidate_keys=changed_duplicate_keys,
            )
        else:
            duplicate_result = refresh_duplicate_candidates(conn, cancel_event, pause_event, failure_callback)
        pruned_duplicates = 0

    return {
        "total_files_scanned": len(image_files),
        "pixiv_artworks_found": len(grouped),
        "new_artworks": new_artworks,
        "new_images": new_images,
        "skipped": skipped,
        "duplicates": duplicates,
        "pruned_duplicates": pruned_duplicates,
        "pruned_images": prune_result["pruned_images"],
        "pruned_artworks": prune_result["pruned_artworks"],
        "duplicate_groups": duplicate_result["groups"],
        "duplicate_images": duplicate_result["images"],
        "source_dir": source_dir,
        "scan_mode": mode,
        "hash_mode": _scan_hash_mode(incremental),
    }


def refresh_duplicate_candidates(
    conn,
    cancel_event=None,
    pause_event=None,
    failure_callback=None,
    candidate_keys=None,
):
    """Hash only files belonging to the same artwork and page."""
    by_artwork_page = defaultdict(list)
    if candidate_keys is None:
        conn.execute("DELETE FROM duplicate_images")
        conn.execute("DELETE FROM duplicate_groups")
        conn.execute("DELETE FROM suspect_images")
        conn.execute("DELETE FROM suspect_groups")
        rows = conn.execute(
            """SELECT i.id, i.artwork_id, i.page, i.path, i.file_name,
                      i.file_size, i.sha256
               FROM images i
               WHERE i.file_size IS NOT NULL"""
        ).fetchall()
        for row in rows:
            if cancel_event and cancel_event.is_set():
                return {"groups": 0, "images": 0, "cancelled": True}
            by_artwork_page[(row["artwork_id"], row["page"])].append(row)
    else:
        for artwork_id, page in candidate_keys:
            conn.execute(
                """DELETE FROM duplicate_groups
                   WHERE group_key IN (
                       SELECT di.group_key
                       FROM duplicate_images di
                       JOIN images i ON i.id = di.image_id
                       WHERE i.artwork_id = ? AND i.page = ?
                   )""",
                (artwork_id, page),
            )
            conn.execute(
                """DELETE FROM suspect_groups
                   WHERE group_key IN (
                       SELECT si.group_key
                       FROM suspect_images si
                       JOIN images i ON i.id = si.image_id
                       WHERE i.artwork_id = ? AND i.page = ?
                   )""",
                (artwork_id, page),
            )
            conn.execute(
                """DELETE FROM suspect_images
                   WHERE image_id IN (
                       SELECT id FROM images WHERE artwork_id = ? AND page = ?
                   )""",
                (artwork_id, page),
            )
            rows = conn.execute(
                """SELECT id, artwork_id, page, path, file_name,
                          file_size, sha256
                   FROM images
                   WHERE artwork_id = ? AND page = ?
                     AND file_size IS NOT NULL""",
                (artwork_id, page),
            ).fetchall()
            if len(rows) > 1:
                by_artwork_page[(artwork_id, page)].extend(rows)

    groups = 0
    images = 0
    for (artwork_id, page), candidates in by_artwork_page.items():
        if len(candidates) < 2:
            continue
        by_hash = defaultdict(list)
        for row in candidates:
            if cancel_event and cancel_event.is_set():
                return {"groups": groups, "images": images, "cancelled": True}
            if not _wait_if_paused(cancel_event, pause_event):
                return {"groups": groups, "images": images, "cancelled": True}
            sha = row["sha256"]
            if not sha:
                try:
                    sha = compute_sha256(row["path"])
                    conn.execute(
                        "UPDATE images SET sha256 = ? WHERE id = ?",
                        (sha, row["id"]),
                    )
                except Exception:
                    if failure_callback:
                        failure_callback("scan", row["path"], "duplicate-hash")
                    continue
            by_hash[sha].append(row["id"])

        for sha, image_ids in by_hash.items():
            if len(image_ids) < 2:
                continue
            file_name = candidates[0]["file_name"] or _file_name_key(candidates[0]["path"])
            file_size = candidates[0]["file_size"]
            group_key = hashlib.sha1(f"{artwork_id}|{page}|{sha}".encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR REPLACE INTO duplicate_groups (group_key, file_name, file_size, sha256, updated_at) VALUES (?, ?, ?, ?, ?)",
                (group_key, file_name, file_size, sha, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            for image_id in image_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO duplicate_images (group_key, image_id, checked) VALUES (?, ?, 1)",
                    (group_key, image_id),
                )
            groups += 1
            images += len(image_ids)
        if len(by_hash) > 1:
            suspect_key = hashlib.sha1(f"{artwork_id}|{page}|suspect".encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT OR REPLACE INTO suspect_groups (group_key, artwork_id, page, updated_at) VALUES (?, ?, ?, ?)",
                (suspect_key, artwork_id, page, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            for row in candidates:
                conn.execute(
                    "INSERT OR REPLACE INTO suspect_images (group_key, image_id, checked) VALUES (?, ?, 0)",
                    (suspect_key, row["id"]),
                )
    conn.commit()
    log.info(
        "duplicate candidate refresh finished: mode=%s keys=%d groups=%d images=%d",
        "full" if candidate_keys is None else "incremental",
        len(by_artwork_page),
        groups,
        images,
    )
    return {"groups": groups, "images": images, "cancelled": False}


def _prune_duplicates(conn):
    """Delete images whose content (sha256) already exists elsewhere in the DB."""
    dup_rows = conn.execute(
        """SELECT img.id, img.artwork_id
           FROM images AS img
           WHERE img.sha256 IS NOT NULL
             AND img.id > (SELECT MIN(id) FROM images AS i2
                           WHERE i2.sha256 = img.sha256)"""
    ).fetchall()
    for row in dup_rows:
        conn.execute("DELETE FROM images WHERE id = ?", (row["id"],))
    return len(dup_rows)


def _prune_missing(conn, source_dir, found_paths):
    """Delete DB records whose image files no longer exist in the source dir."""
    found_set = set(os.path.normcase(os.path.abspath(p)) for p in found_paths)
    source_root = os.path.normcase(os.path.abspath(source_dir))

    pruned_images = 0
    pruned_artworks = 0

    rows = conn.execute("SELECT id, artwork_id, path FROM images").fetchall()
    orphans = []
    for row in rows:
        path = os.path.normcase(os.path.abspath(row["path"]))
        try:
            if os.path.commonpath((path, source_root)) != source_root:
                continue
        except ValueError:
            continue
        if path not in found_set:
            orphans.append((row["id"], row["artwork_id"]))

    for img_id, artwork_id in orphans:
        conn.execute("DELETE FROM images WHERE id = ?", (img_id,))
        pruned_images += 1

    artworks = conn.execute(
        "SELECT id, pixiv_id FROM artworks WHERE id IN "
        "(SELECT DISTINCT artwork_id FROM images WHERE path IS NOT NULL)"
    ).fetchall()

    empty_artworks = conn.execute(
        """SELECT a.id, a.pixiv_id
           FROM artworks a
           WHERE NOT EXISTS (SELECT 1 FROM images i WHERE i.artwork_id = a.id)"""
    ).fetchall()

    for art in empty_artworks:
        conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (art["id"],))
        conn.execute("DELETE FROM artworks WHERE id = ?", (art["id"],))
        _remove_side_files(art["pixiv_id"])
        pruned_artworks += 1

    for art in artworks:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM images WHERE artwork_id = ?", (art["id"],)
        ).fetchone()[0]
        _normalize_image_pages(conn, art["id"])
        cover = conn.execute(
            "SELECT path FROM images WHERE artwork_id = ? ORDER BY page ASC, id ASC LIMIT 1",
            (art["id"],),
        ).fetchone()
        conn.execute(
            "UPDATE artworks SET page_count = ?, local_path = ? WHERE id = ?",
            (remaining, cover["path"] if cover else None, art["id"]),
        )

    return {"pruned_images": pruned_images, "pruned_artworks": pruned_artworks}


def _normalize_image_pages(conn, artwork_id=None):
    """把同一作品内的图片页码重排成 1..N，确保封面稳定指向第一页。"""
    if artwork_id is None:
        rows = conn.execute("SELECT id FROM artworks ORDER BY id").fetchall()
        for row in rows:
            _normalize_image_pages(conn, row["id"])
        return

    images = conn.execute(
        "SELECT id, page, path FROM images WHERE artwork_id = ?",
        (artwork_id,),
    ).fetchall()
    images = sorted(
        images,
        key=lambda img: (
            extract_page_from_filename(img["path"], img["page"]),
            os.path.basename(img["path"]).lower(),
            img["id"],
        ),
    )
    for idx, img in enumerate(images, start=1):
        conn.execute(
            "UPDATE images SET page = ? WHERE id = ?",
            (idx, img["id"]),
        )


def _remove_side_files(pixiv_id):
    thumb_path = os.path.join(paths.DATA_DIR, THUMBNAIL_DIR, f"{pixiv_id}.jpg")
    meta_path = os.path.join(paths.DATA_DIR, METADATA_DIR, f"{pixiv_id}.json")
    for p in (thumb_path, meta_path):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


if __name__ == "__main__":
    source = os.getenv("IMAGE_SOURCE_DIR", "")
    if not source:
        print("Error: IMAGE_SOURCE_DIR not set in .env")
    else:
        result = scan_directory(source)
        for k, v in result.items():
            print(f"  {k}: {v}")
