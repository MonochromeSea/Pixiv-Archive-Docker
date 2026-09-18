import os
import json
import logging
import time
from datetime import datetime
from dotenv import load_dotenv
from app import paths
from app.database import get_db, init_db
from app.pixiv import (
    get_pixiv_client,
    PixivDeletedError,
    PixivAuthError,
    PixivNetworkError,
)
from app.tag_rules import ai_classification, normalize_ai_type

load_dotenv(paths.ENV_FILE)

METADATA_DIR = os.getenv("METADATA_DIR", "metadata")
log = logging.getLogger("pixiv_archive.sync")


def _wait_if_paused(cancel_event=None, pause_event=None):
    while pause_event is not None and pause_event.is_set():
        if cancel_event and cancel_event.is_set():
            return False
        time.sleep(0.2)
    return not (cancel_event and cancel_event.is_set())


def sync_metadata(
    specific_pixiv_id=None,
    specific_pixiv_ids=None,
    progress_callback=None,
    cancel_event=None,
    pause_event=None,
    force_all=False,
    failure_callback=None,
    initialize_db=True,
    commit_each=True,
    commit_batch_size=None,
    batch_callback=None,
):
    if initialize_db:
        init_db()
    client = get_pixiv_client()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 风控限速：默认 800ms，设置里可调（SYNC_DELAY_MS，0=不限速）
    _d_str = (os.getenv("SYNC_DELAY_MS", "") or "").strip()
    delay_ms = int(_d_str) if _d_str.isdigit() else 800
    delay_ms = max(0, min(delay_ms, 10000))
    if commit_batch_size is None:
        commit_batch_size = 1 if commit_each else 8
    try:
        commit_batch_size = max(1, min(int(commit_batch_size), 100))
    except (TypeError, ValueError):
        commit_batch_size = 1 if commit_each else 8

    with get_db() as conn:
        if specific_pixiv_id:
            rows = conn.execute(
                "SELECT id, pixiv_id FROM artworks WHERE pixiv_id = ?",
                (specific_pixiv_id,),
            ).fetchall()
        elif specific_pixiv_ids:
            pixiv_ids = list(dict.fromkeys(int(value) for value in specific_pixiv_ids))
            placeholders = ",".join("?" for _ in pixiv_ids)
            fetched = conn.execute(
                f"SELECT id, pixiv_id FROM artworks WHERE pixiv_id IN ({placeholders})",
                pixiv_ids,
            ).fetchall()
            by_pixiv_id = {row["pixiv_id"]: row for row in fetched}
            rows = [by_pixiv_id[pixiv_id] for pixiv_id in pixiv_ids if pixiv_id in by_pixiv_id]
        elif force_all:
            rows = conn.execute(
                "SELECT id, pixiv_id FROM artworks ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, pixiv_id FROM artworks "
                "WHERE title IS NULL OR last_synced IS NULL "
                   "   OR pixiv_status = 'deleted' OR sync_error IS NOT NULL "
                   "   OR ai_type IS NULL"
            ).fetchall()

        total = len(rows)
        scope = (
            "single" if specific_pixiv_id
            else ("subset" if specific_pixiv_ids else ("full" if force_all else "incremental"))
        )
        results = {
            "synced": 0,
            "ai_detected": 0,
            "failed": 0,
            "deleted": 0,
            "cancelled": False,
            "auth_error": None,
            "details": [],
        }
        pending_details = []
        # 批量管线按批次调用本函数，逐批 INFO 会刷屏；批次级只在 DEBUG 输出。
        _log = log.debug if scope == "subset" else log.info
        _log(
            "metadata sync started: scope=%s artworks=%d delay_ms=%d batch=%d",
            scope, total, delay_ms, commit_batch_size,
        )

        # 预先认证一次：token 失效时立即失败，不把错误逐条写进 artwork.sync_error，
        # 也不要在没有任何成功可能时白跑一整轮。
        if total:
            try:
                client.ensure_auth()
            except PixivAuthError as e:
                results["auth_error"] = str(e)
                log.error("metadata sync aborted before start: pixiv auth failed: %s", e)
                return results

        def commit_pending(force=False):
            if not pending_details or (not force and len(pending_details) < commit_batch_size):
                return
            conn.commit()
            committed = pending_details[:]
            pending_details.clear()
            if batch_callback:
                batch_callback({
                    "details": committed,
                    "synced": sum(1 for item in committed if item["status"] == "synced"),
                    "ai_detected": sum(
                        1 for item in committed
                        if item["status"] == "synced" and item.get("ai_type") == 2
                    ),
                    "failed": sum(1 for item in committed if item["status"] == "failed"),
                    "deleted": sum(1 for item in committed if item["status"] == "deleted"),
                })

        for idx, row in enumerate(rows):
            artwork_id = row["id"]
            pixiv_id = row["pixiv_id"]

            if cancel_event and cancel_event.is_set():
                results["cancelled"] = True
                break
            if not _wait_if_paused(cancel_event, pause_event):
                results["cancelled"] = True
                break

            if progress_callback:
                progress_callback(
                    "sync", idx + 1, total,
                    f"同步元数据…{idx + 1}/{total}（PID {pixiv_id}）",
                )
            # 全量重建可能跑很久，定期在容器日志打点，方便判断是否卡住。
            if total > 50 and (idx + 1) % 25 == 0:
                log.info(
                    "metadata sync progress: %d/%d synced=%d failed=%d",
                    idx + 1, total, results["synced"], results["failed"],
                )

            # 每次请求前按间隔限速（0.1s 分片，随时可取消）
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

            try:
                illust_data = client.get_illust_detail(pixiv_id)
            except PixivAuthError as e:
                # 认证失败：继续请求也会失败，中止整批并回传原因
                log.error(
                    "metadata sync aborted: pixiv auth failed at pixiv_id=%s: %s",
                    pixiv_id, e,
                )
                results["auth_error"] = str(e)
                results["failed"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "failed", "error": str(e)}
                )
                pending_details.append(results["details"][-1])
                if failure_callback:
                    failure_callback("sync", pixiv_id, "auth")
                conn.execute(
                    "UPDATE artworks SET sync_error = ? WHERE id = ?",
                    (str(e)[:300], artwork_id),
                )
                commit_pending(force=True)
                break
            except PixivNetworkError as e:
                # 网络失败：不改作品状态，记录原因，下次同步自动重试
                log.warning(
                    "metadata sync network error at pixiv_id=%s: %s", pixiv_id, e,
                )
                results["failed"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "failed", "error": str(e)}
                )
                pending_details.append(results["details"][-1])
                if failure_callback:
                    failure_callback("sync", pixiv_id, "network")
                conn.execute(
                    "UPDATE artworks SET sync_error = ? WHERE id = ?",
                    (str(e)[:300], artwork_id),
                )
                commit_pending()
                continue
            except PixivDeletedError as e:
                # 仅当 Pixiv 明确返回“作品已删除/不存在”时才标记 deleted
                log.info("metadata sync marked deleted: pixiv_id=%s (%s)", pixiv_id, e)
                conn.execute(
                    "UPDATE artworks SET pixiv_status = 'deleted', last_synced = ?, sync_error = NULL WHERE id = ?",
                    (now_str, artwork_id),
                )
                results["deleted"] += 1
                results["details"].append(
                    {"pixiv_id": pixiv_id, "status": "deleted", "error": str(e)}
                )
                pending_details.append(results["details"][-1])
                commit_pending()
                continue

            author_id = _upsert_author(conn, illust_data)
            tags = illust_data.get("tags", [])
            _upsert_tags(conn, artwork_id, tags)
            # Pixiv clients/proxies may serialize this field as either an
            # integer or a string. Normalize before classification and DB
            # writes so the AI filter behaves consistently in both cases.
            ai_type = normalize_ai_type(illust_data.get("ai_type"))
            illust_data["ai_type"] = ai_type
            ai_source = illust_data.get("ai_type_source") or "unknown"
            # Keep an official Human value authoritative. If Pixiv omitted
            # the field, explicit AI markers can still classify the artwork.
            if ai_type in (None, 0) and ai_classification(ai_type, tags) == "AI":
                ai_type = 2
                ai_source = "explicit-ai-marker"
                illust_data["ai_type"] = ai_type
                illust_data["ai_type_source"] = ai_source

            conn.execute(
                """UPDATE artworks SET
                    title = ?, description = ?, author_id = ?, author_name = ?,
                    create_date = ?, page_count = ?, width = ?, height = ?,
                    ai_type = ?,
                    pixiv_status = 'active', last_synced = ?, sync_error = NULL
                WHERE id = ?""",
                (
                    illust_data["title"],
                    illust_data["description"],
                    author_id,
                    illust_data["author_name"],
                    illust_data["create_date"],
                    illust_data["page_count"],
                    illust_data["width"],
                    illust_data["height"],
                    ai_type,
                    now_str,
                    artwork_id,
                ),
            )

            _save_metadata_json(pixiv_id, illust_data)
            # 逐条成功日志只在 DEBUG 输出；全量同步上千作品时 INFO 会刷屏。
            log.debug(
                "metadata synced: pixiv_id=%s ai_type=%s source=%s",
                pixiv_id,
                ai_type,
                ai_source,
            )
            results["synced"] += 1
            if ai_type == 2:
                results["ai_detected"] += 1
            results["details"].append({"pixiv_id": pixiv_id, "status": "synced"})
            results["details"][-1].update({
                "ai_type": ai_type,
                "ai_type_source": ai_source,
            })
            pending_details.append(results["details"][-1])
            commit_pending()

        commit_pending(force=True)
        _log(
            "metadata sync finished: scope=%s synced=%d ai_detected=%d failed=%d "
            "deleted=%d cancelled=%s auth_error=%s",
            scope, results["synced"], results["ai_detected"], results["failed"],
            results["deleted"], results["cancelled"], bool(results["auth_error"]),
        )
        return results


def _upsert_author(conn, illust_data):
    existing = conn.execute(
        "SELECT id FROM authors WHERE pixiv_user_id = ?",
        (illust_data["author_id"],),
    ).fetchone()

    if existing:
        return existing["id"]

    cursor = conn.execute(
        "INSERT INTO authors (pixiv_user_id, name, profile_image) VALUES (?, ?, ?)",
        (
            illust_data["author_id"],
            illust_data["author_name"],
            illust_data["author_profile_image"],
        ),
    )
    return cursor.lastrowid


def _upsert_tags(conn, artwork_id, tags):
    conn.execute("DELETE FROM artwork_tags WHERE artwork_id = ?", (artwork_id,))
    for tag_data in tags:
        name = tag_data["name"]
        translated = tag_data.get("translated_name") or ""
        existing = conn.execute(
            "SELECT id FROM tags WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            tag_id = existing["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO tags (name, translated_name) VALUES (?, ?)",
                (name, translated if translated else None),
            )
            tag_id = cursor.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO artwork_tags (artwork_id, tag_id) VALUES (?, ?)",
            (artwork_id, tag_id),
        )


def _save_metadata_json(pixiv_id, illust_data):
    metadata_dir = os.path.join(paths.DATA_DIR, METADATA_DIR)
    os.makedirs(metadata_dir, exist_ok=True)
    filepath = os.path.join(metadata_dir, f"{pixiv_id}.json")
    compact = (os.getenv("PA_METADATA_COMPACT", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )
    with open(filepath, "w", encoding="utf-8") as f:
        if compact:
            json.dump(illust_data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(illust_data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    result = sync_metadata()
    print(f"Synced: {result['synced']}, Failed: {result['failed']}, Deleted: {result['deleted']}")
