import sqlite3
import os
import json
import threading
from contextlib import contextmanager
from dotenv import load_dotenv
from app import paths
from app.tag_rules import normalize_ai_type

load_dotenv(paths.ENV_FILE)

DB_PATH = os.path.join(paths.DATA_DIR, "archive.db")
_setup_lock = threading.Lock()
_wal_configured = False
_init_lock = threading.Lock()
_initialized = False


def get_connection():
    global _wal_configured
    # timeout=30：任务（同步/扫描）持有写事务期间，UI 发起的并发写等待锁而不是 5 秒即报
    # "database is locked"
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    if not _wal_configured:
        with _setup_lock:
            if not _wal_configured:
                conn.execute("PRAGMA journal_mode=WAL")
                _wal_configured = True
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    conn.execute("PRAGMA cache_size=-4096")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    global _initialized
    if _initialized and os.path.exists(DB_PATH):
        return
    with _init_lock:
        if _initialized and os.path.exists(DB_PATH):
            return
        conn = get_connection()
        cursor = conn.cursor()

        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS authors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pixiv_user_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            profile_image TEXT
        );

        CREATE TABLE IF NOT EXISTS artworks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pixiv_id INTEGER UNIQUE NOT NULL,
            title TEXT,
            description TEXT,
            author_id INTEGER,
            author_name TEXT,
            create_date TEXT,
            page_count INTEGER DEFAULT 1,
            width INTEGER,
            height INTEGER,
            ai_type INTEGER,
            pixiv_status TEXT DEFAULT 'active',
            first_seen TEXT DEFAULT (datetime('now')),
            last_synced TEXT,
            local_path TEXT,
            FOREIGN KEY (author_id) REFERENCES authors(id)
        );

        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            translated_name TEXT
        );

        CREATE TABLE IF NOT EXISTS artwork_tags (
            artwork_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            PRIMARY KEY (artwork_id, tag_id),
            FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artwork_id INTEGER NOT NULL,
            page INTEGER NOT NULL,
            path TEXT NOT NULL,
            file_name TEXT,
            file_mtime_ns INTEGER,
            file_size INTEGER,
            width INTEGER,
            height INTEGER,
            sha256 TEXT,
            phash TEXT,
            FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS duplicate_groups (
            group_key TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            sha256 TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS duplicate_images (
            group_key TEXT NOT NULL,
            image_id INTEGER NOT NULL,
            checked INTEGER DEFAULT 1,
            PRIMARY KEY (group_key, image_id),
            FOREIGN KEY (group_key) REFERENCES duplicate_groups(group_key) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS favorites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            create_date TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS favorite_artworks (
            favorite_id INTEGER NOT NULL,
            artwork_id INTEGER NOT NULL,
            added_date TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (favorite_id, artwork_id),
            FOREIGN KEY (favorite_id) REFERENCES favorites(id) ON DELETE CASCADE,
            FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bookmark_subs (
            pixiv_user_id INTEGER PRIMARY KEY,
            name TEXT,
            last_pid INTEGER,
            auto_download INTEGER DEFAULT 1,
            last_checked TEXT,
            last_result TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_artworks_pixiv_id ON artworks(pixiv_id);
        CREATE INDEX IF NOT EXISTS idx_artworks_author_id ON artworks(author_id);
        CREATE INDEX IF NOT EXISTS idx_artworks_title ON artworks(title);
        CREATE INDEX IF NOT EXISTS idx_images_artwork_id ON images(artwork_id);
        CREATE INDEX IF NOT EXISTS idx_images_path ON images(path);
        CREATE INDEX IF NOT EXISTS idx_images_artwork_page ON images(artwork_id, page);
        CREATE INDEX IF NOT EXISTS idx_images_sha256 ON images(sha256);
        CREATE INDEX IF NOT EXISTS idx_duplicate_images_group ON duplicate_images(group_key);
        CREATE INDEX IF NOT EXISTS idx_duplicate_images_image ON duplicate_images(image_id);
        CREATE INDEX IF NOT EXISTS idx_artwork_tags_artwork_id ON artwork_tags(artwork_id);
        CREATE INDEX IF NOT EXISTS idx_artwork_tags_tag_id ON artwork_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name);
        CREATE INDEX IF NOT EXISTS idx_favorite_artworks_favorite ON favorite_artworks(favorite_id);
        CREATE INDEX IF NOT EXISTS idx_favorite_artworks_artwork ON favorite_artworks(artwork_id);
        """)

        _migrate_sync_error(conn)
        _migrate_artwork_ai_type(conn)
        _migrate_image_file_stat(conn)
        _migrate_duplicate_tables(conn)
        _migrate_drop_artist_subs(conn)
        _migrate_bookmark_subs_col(conn)

        conn.commit()
        conn.close()
        _initialized = True


def _migrate_bookmark_subs_col(conn):
    """bookmark_subs 游标列语义修正：last_bid → last_pid（收藏项不含 bookmark_data.id，
    改用最大作品 PID 作增量游标）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bookmark_subs)").fetchall()}
    if "last_bid" in cols and "last_pid" not in cols:
        conn.execute("ALTER TABLE bookmark_subs RENAME COLUMN last_bid TO last_pid")


def _migrate_drop_artist_subs(conn):
    """v1.2.1：画师订阅功能已被「收藏订阅」(bookmark_subs) 取代，旧表数据丢弃。"""
    conn.execute("DROP TABLE IF EXISTS subscriptions")


def _migrate_sync_error(conn):
    """为旧库补上 sync_error 列（记录最近一次同步失败原因，用于下次重试与提示）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(artworks)").fetchall()}
    if "sync_error" not in cols:
        conn.execute("ALTER TABLE artworks ADD COLUMN sync_error TEXT")


def _migrate_artwork_ai_type(conn):
    """Store Pixiv AI status and backfill existing metadata exactly once."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(artworks)").fetchall()}
    if "ai_type" not in cols:
        conn.execute("ALTER TABLE artworks ADD COLUMN ai_type INTEGER")
    migration_key = "ai_type_metadata_backfill_v1"
    if conn.execute(
        "SELECT 1 FROM app_meta WHERE key = ?", (migration_key,)
    ).fetchone():
        return
    metadata_dir = os.path.join(paths.DATA_DIR, os.getenv("METADATA_DIR", "metadata"))
    if not os.path.isdir(metadata_dir):
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, datetime('now'))",
            (migration_key,),
        )
        return
    rows = conn.execute(
        "SELECT id, pixiv_id FROM artworks WHERE ai_type IS NULL"
    ).fetchall()
    for row in rows:
        path = os.path.join(metadata_dir, f"{row['pixiv_id']}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            value = None
            for key in ("ai_type", "illust_ai_type", "aiType", "ai-type"):
                if data.get(key) is not None:
                    value = normalize_ai_type(data[key])
                    break
            if value is not None:
                conn.execute(
                    "UPDATE artworks SET ai_type = ? WHERE id = ?",
                    (value, row["id"]),
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    conn.execute(
        "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, datetime('now'))",
        (migration_key,),
    )


def _migrate_image_file_stat(conn):
    """为图片表补充文件 stat，支持增量扫描时跳过未变化文件。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(images)").fetchall()}
    if "file_name" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN file_name TEXT")
    if "file_mtime_ns" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN file_mtime_ns INTEGER")
    if "file_size" not in cols:
        conn.execute("ALTER TABLE images ADD COLUMN file_size INTEGER")
    rows = conn.execute("SELECT id, path FROM images WHERE file_name IS NULL").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE images SET file_name = ? WHERE id = ?",
            (os.path.basename(row["path"]).lower(), row["id"]),
        )


def _migrate_duplicate_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS duplicate_groups (
            group_key TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            sha256 TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS duplicate_images (
            group_key TEXT NOT NULL,
            image_id INTEGER NOT NULL,
            checked INTEGER DEFAULT 1,
            PRIMARY KEY (group_key, image_id),
            FOREIGN KEY (group_key) REFERENCES duplicate_groups(group_key) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_images_file_name_size ON images(file_name, file_size);
        CREATE INDEX IF NOT EXISTS idx_duplicate_images_group ON duplicate_images(group_key);
        CREATE INDEX IF NOT EXISTS idx_duplicate_images_image ON duplicate_images(image_id);
        CREATE TABLE IF NOT EXISTS suspect_groups (
            group_key TEXT PRIMARY KEY,
            artwork_id INTEGER NOT NULL,
            page INTEGER NOT NULL,
            updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (artwork_id) REFERENCES artworks(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS suspect_images (
            group_key TEXT NOT NULL,
            image_id INTEGER NOT NULL,
            checked INTEGER DEFAULT 0,
            PRIMARY KEY (group_key, image_id),
            FOREIGN KEY (group_key) REFERENCES suspect_groups(group_key) ON DELETE CASCADE,
            FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_suspect_images_group ON suspect_images(group_key);
        CREATE INDEX IF NOT EXISTS idx_suspect_images_image ON suspect_images(image_id);
    """)
