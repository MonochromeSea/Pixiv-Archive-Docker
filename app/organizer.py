import json
import logging
import os
import re
import shutil
from datetime import datetime

from app import paths
from app.database import get_db
from app.tag_rules import ai_classification, is_r18_tags


UNDO_FILE = os.path.join(paths.DATA_DIR, "organize_last.json")
log = logging.getLogger("pixiv_archive.organizer")


def _safe_name(value, fallback):
    text = (value or "").strip() or fallback
    for ch in '<>:"/\\|?*\r\n\t':
        text = text.replace(ch, "_")
    text = text.strip(" .")
    return text[:120] or fallback


def _is_child(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except Exception:
        return False


def _classify(tags, ai_type=None, unknown_as_human=True):
    r18 = is_r18_tags(tags)
    origin = ai_classification(ai_type, tags)
    if origin == "Unknown" and unknown_as_human:
        origin = "Human"
    return ("R18" if r18 else "SFW", origin)


def _path_has_ai_classification(path_rule, path_template=""):
    if path_rule in {"bucket", "shaft_modern"}:
        return True
    if path_rule == "custom":
        return bool(re.search(r"\[\?!?AI:", str(path_template or "")))
    return False


def _path_classification(path):
    """Infer unavailable Pixiv metadata from explicit directory names only."""
    rating = None
    origin = None
    matched_index = -1
    parts = [part for part in os.path.normpath(path).split(os.sep) if part]
    for index, part in enumerate(parts[:-1]):
        token = re.sub(r"[^a-z0-9]+", "", part.casefold())
        if token in {"r18", "r18g"}:
            rating = "R18"
            matched_index = max(matched_index, index)
        elif token == "sfw":
            rating = "SFW"
            matched_index = max(matched_index, index)
        elif token == "ai":
            origin = "AI"
            matched_index = max(matched_index, index)
        elif token in {"human", "humen"}:
            origin = "Human"
            matched_index = max(matched_index, index)
    return rating, origin, parts, matched_index


def _source_relative_parts(path, source_dirs):
    absolute = os.path.abspath(path)
    matches = []
    for source_dir in source_dirs:
        if not _is_child(absolute, source_dir):
            continue
        relative = os.path.relpath(absolute, source_dir)
        matches.append((len(os.path.abspath(source_dir)), source_dir, relative))
    if not matches:
        return "Source", [os.path.basename(absolute)]
    _, source_dir, relative = max(matches, key=lambda item: item[0])
    source_name = os.path.basename(os.path.normpath(source_dir)) or "Source"
    return source_name, [part for part in relative.split(os.sep) if part]


def _unavailable_destination(output_dir, path, source_dirs):
    """Route deleted/inaccessible works by path without inventing metadata."""
    rating, origin, absolute_parts, matched_index = _path_classification(path)
    source_name, relative_parts = _source_relative_parts(path, source_dirs)
    relative_dirs = relative_parts[:-1]
    if not rating and not origin:
        parts = ["手动处理", source_name, *relative_dirs]
        return os.path.join(output_dir, *[_safe_name(p, "Unknown") for p in parts]), {
            "mode": "manual",
            "rating": None,
            "origin": None,
        }

    # Preserve the useful path suffix after the final classification folder,
    # such as the existing artist/work directories.
    suffix_dirs = absolute_parts[matched_index + 1:-1] if matched_index >= 0 else relative_dirs
    parts = [rating or "待判断", origin or "待判断", *suffix_dirs]
    return os.path.join(output_dir, *[_safe_name(p, "Unknown") for p in parts]), {
        "mode": "path",
        "rating": rating,
        "origin": origin,
    }


def _unique_dest(path):
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path)
    idx = 2
    while True:
        candidate = f"{base} ({idx}){ext}"
        if not os.path.lexists(candidate):
            return candidate
        idx += 1


def _ensure_dir(path, created_dirs=None):
    missing = []
    cursor = path
    while cursor and not os.path.exists(cursor):
        missing.append(cursor)
        next_cursor = os.path.dirname(cursor)
        if next_cursor == cursor:
            break
        cursor = next_cursor
    os.makedirs(path, exist_ok=True)
    if created_dirs is not None:
        created_dirs.extend(reversed(missing))


def _copy_or_link(src, dst, mode, created_dirs=None):
    _ensure_dir(os.path.dirname(dst), created_dirs)
    if mode == "copy":
        shutil.copy2(src, dst)
    elif mode == "move":
        shutil.move(src, dst)
    elif mode == "symlink":
        os.symlink(src, dst)
    elif mode == "hardlink":
        os.link(src, dst)
    else:
        raise ValueError("unsupported organize mode")


PATH_RULES = {
    "shaft_classic": "Shaft 经典（兼容 4.5.8 之前）",
    "shaft_modern": "Shaft 现代（R18/AI 嵌套）",
    "shaft_flat": "扁平（不按作者）",
    "shaft_date": "按日期分组",
    "shaft_artist": "按作者分组",
    "shaft_artist_date": "作者 + 年月分组",
    "shaft_artist_work": "按作者分组 + 多P建子文件夹",
    "shaft_id": "极简（只用 ID）",
    "bucket": "R18 / AI 强制分桶",
    "shaft_detail": "详细（标题+ID+P+画师+尺寸+时间）",
    "custom": "自定义模板",
}

RENAME_RULES = {
    "keep": "保持原文件名",
    "shaft_classic": "标题_ID_页码",
    "shaft_modern": "标题 ID 页码",
    "title_author_page": "标题 作者 页码",
    "shaft_flat": "标题 ID 页码",
    "shaft_date": "标题 ID 页码",
    "shaft_artist": "标题 ID 页码",
    "shaft_artist_date": "标题 ID 页码",
    "shaft_artist_work": "标题 ID 页码",
    "shaft_id": "仅 ID 页码",
    "bucket": "标题 ID 页码",
    "shaft_detail": "详细信息",
    "custom": "自定义模板",
}

SHAFT_PATH_TEMPLATES = {
    # Path templates describe directories only. File names are handled by
    # the source basename or the optional rename template below.
    "bucket": "[?R18:R18/][?!R18:SFW/][?AI:AI/][?!AI:Human/]{author} ({author_id})",
    "shaft_classic": "ShaftImages",
    "shaft_modern": "Shaft/[?R18:R18/][?!R18:SFW/][?AI:AI/][?!AI:Human/]{author}_{author_id}",
    "shaft_flat": "Shaft",
    "shaft_date": "Shaft/{created:yyyy}/{created:yyyy-MM}",
    "shaft_artist": "Shaft/{author}_{author_id}",
    "shaft_artist_date": "Shaft/{author}_{author_id}/{created:yyyy}/{created:yyyy-MM}",
    "shaft_artist_work": "Shaft/{author}_{author_id}/{title} {id}",
    "shaft_id": "Shaft",
    "shaft_detail": "ShaftImages/{title}_{id}",
}

RENAME_TEMPLATES = {
    "shaft_classic": "{title}_{id}_p{page}.{ext}",
    "shaft_modern": "{title} {id} p{page}.{ext}",
    "title_author_page": "{title} {author} p{page}.{ext}",
    "shaft_flat": "{title} {id}_p{page}.{ext}",
    "shaft_date": "{title} {id}_p{page}.{ext}",
    "shaft_artist": "{title} {id}_p{page}.{ext}",
    "shaft_artist_date": "{title} {id}_p{page}.{ext}",
    "shaft_artist_work": "{title} {id}_p{page}.{ext}",
    "shaft_id": "{id} p{page}.{ext}",
    "bucket": "{title} {id}_p{page}.{ext}",
    "shaft_detail": "{title}_{id}_{page}_{author}_{w}x{h}_{created:yyyyMMdd_HHmmss}.{ext}",
}


def _directory_template(template):
    """Keep compatibility with old custom templates that ended in a filename."""
    text = str(template or "").strip()
    parts = [part for part in re.split(r"[/\\]+", text) if part]
    if len(parts) > 1 and re.search(
        r"\{(?:filename|page|pages|ext|w|h)(?::[^{}]+)?\}", parts[-1]
    ):
        parts.pop()
    return "/".join(parts)


def _created_datetime(value):
    text = str(value or "").strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:length], fmt)
        except ValueError:
            pass
    return None


def _format_created(value, pattern):
    dt = _created_datetime(value)
    if not dt:
        return "Unknown Date"
    strftime_pattern = (
        pattern.replace("yyyy", "%Y")
        .replace("MM", "%m")
        .replace("dd", "%d")
        .replace("HH", "%H")
        .replace("mm", "%M")
        .replace("ss", "%S")
    )
    return dt.strftime(strftime_pattern)


def _template_values(row, rating, origin):
    filename = os.path.basename(row["path"])
    ext = os.path.splitext(filename)[1].lstrip(".").lower() or "jpg"
    author = _safe_name(row.get("author_name"), "Unknown Artist")
    author_id = str(row.get("author_id") or "0")
    title = _safe_name(row.get("title"), f"Pixiv {row.get('pixiv_id') or 'Unknown'}")
    page = int(row.get("page") or 1)
    pages = int(row.get("page_count") or page)
    return {
        "filename": filename,
        "title": title,
        "id": str(row.get("pixiv_id") or "0"),
        "page": str(page),
        "pages": str(pages),
        "ext": ext,
        "author": author,
        "author_id": author_id,
        "w": str(row.get("width") or 0),
        "h": str(row.get("height") or 0),
        "r18": rating,
        "ai": origin,
        "created": row.get("create_date") or "",
    }


def render_template(template, row, rating, origin):
    """Render Shaft-style variables and conditional blocks."""
    values = _template_values(row, rating, origin)
    template = str(template or "").strip()

    def condition(match):
        expr, content = match.group(1), match.group(2)
        negated = expr.startswith("!")
        expr = expr[1:] if negated else expr
        if expr == "R18":
            result = rating == "R18"
        elif expr == "AI":
            result = origin == "AI"
        elif expr in ("p>1", "page>1"):
            result = int(values["page"]) > 1
        elif expr in ("p=1", "page=1"):
            result = int(values["page"]) == 1
        else:
            result = False
        return content if (not result if negated else result) else ""

    template = re.sub(r"\[\?([!A-Za-z0-9=><]+):([^\]]*)\]", condition, template)

    def variable(match):
        key = match.group(1)
        if key.startswith("created:"):
            return _format_created(values["created"], key.split(":", 1)[1])
        return str(values.get(key, match.group(0)))

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*(?::[^{}]+)?)\}", variable, template)


def _safe_template_path(rendered):
    parts = []
    for part in re.split(r"[/\\]+", rendered):
        part = _safe_name(part, "Unknown")
        if part not in (".", ".."):
            parts.append(part)
    return os.path.join(*parts) if parts else "Unknown"


def _author_dir(row):
    author = _safe_name(row.get("author_name"), "Unknown Artist")
    author_id = row.get("author_id")
    return f"{author} ({author_id})" if author_id else author


def _date_parts(value):
    text = str(value or "").strip()
    match = re.search(r"(\d{4})[-/](\d{1,2})", text)
    if match:
        return match.group(1), f"{int(match.group(2)):02d}"
    return "Unknown Date", ""


def _build_destination(output_dir, row, rating, origin, path_rule, path_template=""):
    author = _author_dir(row)
    if path_rule == "custom":
        directory_template = _directory_template(path_template)
        relative = _safe_template_path(
            render_template(
                directory_template or SHAFT_PATH_TEMPLATES["shaft_modern"],
                row,
                rating,
                origin,
            )
        )
        return os.path.join(output_dir, relative)
    if path_rule in SHAFT_PATH_TEMPLATES:
        relative = _safe_template_path(
            render_template(SHAFT_PATH_TEMPLATES[path_rule], row, rating, origin)
        )
        return os.path.join(output_dir, relative)
    if path_rule == "artist":
        parts = [author]
    elif path_rule == "rating":
        parts = [rating, author]
    elif path_rule == "ai":
        parts = [origin, author]
    elif path_rule == "date":
        year, month = _date_parts(row.get("create_date"))
        parts = [year] + ([month] if month else []) + [author]
    elif path_rule == "pixiv":
        parts = [author, str(row.get("pixiv_id") or "Unknown Pixiv ID")]
    else:
        parts = [rating, origin, author]
    return os.path.join(output_dir, *[_safe_name(p, "Unknown") for p in parts])


def organize_files(
    source_dirs,
    output_dir,
    mode,
    path_rule="bucket",
    path_template="",
    progress_callback=None,
    cancel_event=None,
    unknown_as_human=True,
    rename_enabled=False,
    rename_rule="title_author_page",
    rename_template="",
    unavailable_pixiv_ids=None,
):
    if isinstance(source_dirs, (str, os.PathLike)):
        source_dirs = [source_dirs]
    source_dirs = [os.path.abspath(str(d)) for d in source_dirs if str(d).strip()]
    output_dir = os.path.abspath(output_dir)
    unavailable_pixiv_ids = {
        int(value) for value in (unavailable_pixiv_ids or []) if value is not None
    }
    unknown_as_human = bool(unknown_as_human) and _path_has_ai_classification(
        path_rule, path_template
    )
    if mode not in {"copy", "move", "symlink", "hardlink"}:
        return {"error": "unsupported mode"}
    if path_rule not in PATH_RULES:
        return {"error": "unsupported path rule"}
    if path_rule == "custom" and not str(path_template or "").strip():
        return {"error": "custom path template is empty"}
    if rename_rule not in RENAME_RULES:
        return {"error": "unsupported rename rule"}
    if rename_enabled and rename_rule == "custom" and not str(rename_template or "").strip():
        return {"error": "custom rename template is empty"}
    missing = [d for d in source_dirs if not os.path.isdir(d)]
    if missing:
        return {"error": f"source directory not found: {missing[0]}"}
    if not source_dirs:
        return {"error": "source directory is empty"}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT i.id AS image_id, i.path, i.page,
                      a.id AS artwork_id, a.pixiv_id, a.author_name,
                      a.author_id, a.create_date, a.title, a.ai_type,
                      a.page_count, a.width, a.height
               FROM images i
               JOIN artworks a ON a.id = i.artwork_id
               ORDER BY a.id ASC, i.page ASC, i.id ASC"""
        ).fetchall()
        items = []
        handled_unavailable_pixiv_ids = set()
        for row in rows:
            if not any(_is_child(row["path"], source_dir) for source_dir in source_dirs):
                continue
            if _is_child(row["path"], output_dir):
                continue
            row_data = dict(row)
            if row["pixiv_id"] in unavailable_pixiv_ids:
                handled_unavailable_pixiv_ids.add(int(row["pixiv_id"]))
                items.append((row_data, None, None, True))
                continue
            tags = conn.execute(
                """SELECT t.name, t.translated_name
                   FROM tags t
                   JOIN artwork_tags at ON at.tag_id = t.id
                   WHERE at.artwork_id = ?""",
                (row["artwork_id"],),
            ).fetchall()
            rating, origin = _classify(
                [dict(t) for t in tags],
                row["ai_type"],
                unknown_as_human=unknown_as_human,
            )
            items.append((row_data, rating, origin, False))

    total = len(items)
    if total == 0:
        return {
            "total": 0,
            "done": 0,
            "failed": 0,
            "mode": mode,
            "cancelled": False,
            "error": "源文件夹中没有找到已入库的图片",
            "unavailable_artworks": len(handled_unavailable_pixiv_ids),
            "path_fallback_files": 0,
            "manual_files": 0,
        }
    created_dirs = []
    if not os.path.exists(output_dir):
        _ensure_dir(output_dir, created_dirs)
    done = 0
    failed = 0
    classification_counts = {"AI": 0, "Human": 0, "Unknown": 0}
    path_fallback_files = 0
    manual_files = 0
    operations = []
    for row, rating, origin, unavailable in items:
        if not unavailable:
            classification_counts[origin] = classification_counts.get(origin, 0) + 1
        if cancel_event and cancel_event.is_set():
            break
        src = row["path"]
        if not os.path.isfile(src):
            failed += 1
            continue
        fallback = None
        if unavailable:
            dst_dir, fallback = _unavailable_destination(
                output_dir, src, source_dirs
            )
            if fallback["mode"] == "manual":
                manual_files += 1
            else:
                path_fallback_files += 1
        else:
            dst_dir = _build_destination(
                output_dir, row, rating, origin, path_rule, path_template
            )
        # Path templates only select the destination directory. The old
        # organizer behavior keeps the source basename unless renaming is
        # explicitly enabled.
        dst = os.path.join(dst_dir, os.path.basename(src))
        if not unavailable and rename_enabled and rename_rule != "keep":
            # A saved custom template must not override a subsequently selected
            # preset. Only the custom rule is allowed to consume this value.
            rename_tpl = (
                rename_template
                if rename_rule == "custom"
                else RENAME_TEMPLATES.get(rename_rule, "")
            )
            rendered_name = _safe_template_path(
                render_template(rename_tpl, row, rating, origin)
            )
            dst = os.path.join(dst_dir, os.path.basename(rendered_name))
        dst = _unique_dest(dst)
        try:
            _copy_or_link(src, dst, mode, created_dirs)
            operations.append({
                "mode": mode,
                "src": src,
                "dst": dst,
                "image_id": row["image_id"],
                "artwork_id": row["artwork_id"],
                "fallback": fallback,
            })
            if mode == "move":
                with get_db() as conn:
                    conn.execute("UPDATE images SET path = ?, file_name = ? WHERE id = ?",
                                 (dst, os.path.basename(dst).lower(), row["image_id"]))
                    conn.execute("UPDATE artworks SET local_path = ? WHERE id = ? AND local_path = ?",
                                 (dst, row["artwork_id"], src))
            done += 1
        except Exception as e:
            failed += 1
            operations.append({
                "mode": mode,
                "src": src,
                "dst": dst,
                "image_id": row["image_id"],
                "artwork_id": row["artwork_id"],
                "error": str(e),
            })
        if progress_callback:
            progress_callback("organize", done + failed, total, f"整理文件...{done + failed}/{total}")

    undo = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_dirs": source_dirs,
        "output_dir": output_dir,
        "mode": mode,
        "path_rule": path_rule,
        "path_template": path_template,
        "rename_enabled": bool(rename_enabled),
        "rename_rule": rename_rule,
        "rename_template": rename_template,
        "created_dirs": list(dict.fromkeys(created_dirs)),
        "operations": operations,
    }
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(UNDO_FILE, "w", encoding="utf-8") as f:
        json.dump(undo, f, ensure_ascii=False, indent=2)
    result = {
        "total": total,
        "done": done,
        "failed": failed,
        "mode": mode,
        "cancelled": bool(cancel_event and cancel_event.is_set()),
        "classification_counts": classification_counts,
        "source_dirs": source_dirs,
        "output_dir": output_dir,
        "path_rule": path_rule,
        "unavailable_artworks": len(handled_unavailable_pixiv_ids),
        "unavailable_pixiv_ids": sorted(handled_unavailable_pixiv_ids),
        "path_fallback_files": path_fallback_files,
        "manual_files": manual_files,
    }
    log.info(
        "organize classification: AI=%d Human=%d Unknown=%d",
        classification_counts.get("AI", 0),
        classification_counts.get("Human", 0),
        classification_counts.get("Unknown", 0),
    )
    if handled_unavailable_pixiv_ids:
        log.warning(
            "organize unavailable metadata fallback: artworks=%d path_files=%d "
            "manual_files=%d pixiv_ids=%s",
            len(handled_unavailable_pixiv_ids),
            path_fallback_files,
            manual_files,
            sorted(handled_unavailable_pixiv_ids),
        )
    return result


def undo_last_organize(progress_callback=None, cancel_event=None):
    if not os.path.exists(UNDO_FILE):
        return {"error": "no undo record"}
    with open(UNDO_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    ops = [op for op in data.get("operations", []) if not op.get("error")]
    total = len(ops)
    done = 0
    failed = 0
    directory_cleanup_failed = 0
    directories_removed = 0
    for op in reversed(ops):
        if cancel_event and cancel_event.is_set():
            break
        try:
            mode = op.get("mode")
            src = op.get("src")
            dst = op.get("dst")
            if mode == "move":
                if dst and os.path.exists(dst):
                    if os.path.exists(src):
                        raise FileExistsError(f"恢复目标已存在: {src}")
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    shutil.move(dst, src)
                with get_db() as conn:
                    conn.execute("UPDATE images SET path = ?, file_name = ? WHERE id = ?",
                                 (src, os.path.basename(src).lower(), op.get("image_id")))
                    conn.execute(
                        "UPDATE artworks SET local_path = ? WHERE id = ? AND local_path = ?",
                        (src, op.get("artwork_id"), dst),
                    )
            else:
                if dst and os.path.lexists(dst):
                    os.remove(dst)
            done += 1
        except Exception:
            failed += 1
        if progress_callback:
            progress_callback("organize", done + failed, total, f"撤销整理...{done + failed}/{total}")
    cancelled = bool(cancel_event and cancel_event.is_set())
    verified = 0
    verification_failed = 0
    for op in ops:
        src = op.get("src")
        dst = op.get("dst")
        mode = op.get("mode")
        if mode == "move":
            ok = os.path.isfile(src) and not os.path.lexists(dst)
        else:
            ok = not os.path.lexists(dst)
        if ok:
            verified += 1
        else:
            verification_failed += 1
    for directory in sorted(set(data.get("created_dirs", [])), key=lambda p: len(str(p)), reverse=True):
        try:
            if os.path.isdir(directory) and not os.listdir(directory):
                os.rmdir(directory)
                directories_removed += 1
            elif os.path.isdir(directory):
                directory_cleanup_failed += 1
        except OSError:
            directory_cleanup_failed += 1
    status = "cancelled" if cancelled else (
        "success" if failed == 0 and verification_failed == 0 and directory_cleanup_failed == 0 else "failed"
    )
    result = {
        "total": total,
        "done": done,
        "failed": failed,
        "verified": verified,
        "verification_failed": verification_failed,
        "directory_cleanup_failed": directory_cleanup_failed,
        "directories_removed": directories_removed,
        "status": status,
        "mode": "undo",
        "cancelled": cancelled,
    }
    try:
        data["last_undo_result"] = result
        with open(UNDO_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        log.warning("unable to persist organize undo verification", exc_info=True)
    return result
