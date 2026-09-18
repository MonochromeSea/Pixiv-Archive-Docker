import os
import hashlib
import logging
import tempfile
from PIL import Image
from dotenv import load_dotenv
from app import paths

load_dotenv(paths.ENV_FILE)

THUMBNAIL_SIZE = int(os.getenv("THUMBNAIL_SIZE", "400"))
THUMBNAIL_DIR = os.getenv("THUMBNAIL_DIR", "thumbnails")
log = logging.getLogger("pixiv_archive.thumbnails")


def get_thumbnail_dir():
    return os.path.join(paths.DATA_DIR, THUMBNAIL_DIR)


def _image_thumbnail_path(source_path):
    try:
        stat = os.stat(source_path)
        version = f"{os.path.abspath(source_path)}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        version = os.path.abspath(source_path)
    key = hashlib.sha1(version.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return os.path.join(get_thumbnail_dir(), "images", f"{key}.jpg")


def generate_image_thumbnail(source_path, force=False):
    """Generate an on-demand thumbnail for one registered source image."""
    thumb_path = _image_thumbnail_path(source_path)
    os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    if os.path.exists(thumb_path) and not force:
        try:
            if os.path.getsize(thumb_path) > 0:
                return thumb_path
            os.remove(thumb_path)
        except OSError:
            pass
    temp_file = tempfile.NamedTemporaryFile(
        prefix=os.path.basename(thumb_path) + ".",
        suffix=".tmp",
        dir=os.path.dirname(thumb_path),
        delete=False,
    )
    temp_path = temp_file.name
    temp_file.close()
    try:
        with Image.open(source_path) as source:
            source.draft("RGB", (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
            img = source.convert("RGB")
        img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.LANCZOS)
        img.save(temp_path, "JPEG", quality=82, optimize=False)
        img.close()
        os.replace(temp_path, thumb_path)
        return thumb_path
    except Exception:
        log.exception("image thumbnail generation failed: source=%s", source_path)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return None

def generate_thumbnail(source_path, pixiv_id, force=False):
    thumb_dir = get_thumbnail_dir()
    os.makedirs(thumb_dir, exist_ok=True)

    thumb_path = os.path.join(thumb_dir, f"{pixiv_id}.jpg")

    if os.path.exists(thumb_path) and not force:
        try:
            if os.path.getsize(thumb_path) > 0:
                return thumb_path
            os.remove(thumb_path)
        except OSError:
            pass

    # Write beside the final file and replace atomically so browser requests
    # never observe a half-written JPEG during a refresh.
    temp_file = tempfile.NamedTemporaryFile(
        prefix=f"{pixiv_id}.", suffix=".tmp", dir=thumb_dir, delete=False
    )
    temp_path = temp_file.name
    temp_file.close()
    try:
        with Image.open(source_path) as source:
            source.draft("RGB", (THUMBNAIL_SIZE, THUMBNAIL_SIZE))
            img = source.convert("RGB")
        img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE), Image.LANCZOS)
        img.save(temp_path, "JPEG", quality=85, optimize=False)
        img.close()
        os.replace(temp_path, thumb_path)
        return thumb_path
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return None


def generate_thumbnails_for_artwork(artwork_id, pixiv_id, image_paths):
    if not image_paths:
        return None
    return generate_thumbnail(image_paths[0], pixiv_id)


def _select_cover_rows(conn):
    return conn.execute(
        """SELECT a.id, a.pixiv_id, i.path
           FROM artworks a
           JOIN images i ON i.id = (
               SELECT i2.id
               FROM images i2
               WHERE i2.artwork_id = a.id
               ORDER BY CASE WHEN i2.page = 1 THEN 0 ELSE 1 END,
                        i2.page ASC,
                        i2.id ASC
               LIMIT 1
           )"""
    ).fetchall()


def generate_all_thumbnails(conn, progress_callback=None, cancel_event=None):
    rows = _select_cover_rows(conn)

    results = {"generated": 0, "skipped": 0, "failed": 0, "cancelled": False}
    total = len(rows)
    for i, row in enumerate(rows):
        if cancel_event and cancel_event.is_set():
            results["cancelled"] = True
            break
        if progress_callback:
            progress_callback("thumb", i + 1, total, f"生成缩略图…{i + 1}/{total}")
        pixiv_id = row["pixiv_id"]
        source_path = row["path"]
        thumb_path = generate_thumbnail(source_path, pixiv_id, force=False)
        if thumb_path:
            if os.path.getsize(thumb_path) > 0:
                results["generated"] += 1
            else:
                results["failed"] += 1
        else:
            results["failed"] += 1

    return results


if __name__ == "__main__":
    from app.database import get_db, init_db
    init_db()
    with get_db() as conn:
        results = generate_all_thumbnails(conn)
        print(results)
