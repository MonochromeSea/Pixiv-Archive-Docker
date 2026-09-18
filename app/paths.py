"""集中式路径管理。

- DATA_DIR：可写数据目录。打包后（PyInstaller）= exe 同级目录（便携模式）；
  源码运行时 = 项目根目录。
- RES_DIR：只读打包资源根目录。打包后 = sys._MEIPASS；源码运行时 = 项目根目录。
- APP_DIR：app 包所在目录（内含 static/ 与 templates/）。
- ENV_FILE：位于 DATA_DIR 内的 .env 路径。
"""
import os
import sys


def _is_frozen():
    return bool(getattr(sys, "frozen", False))


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_data_dir():
    env_data_dir = (os.getenv("PA_DATA_DIR", "") or "").strip()
    if env_data_dir:
        return os.path.abspath(env_data_dir)
    if _is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return _project_root()


def get_res_dir():
    if _is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return meipass
        return os.path.dirname(os.path.abspath(sys.executable))
    return _project_root()


def get_app_dir():
    return os.path.join(get_res_dir(), "app")


DATA_DIR = get_data_dir()
RES_DIR = get_res_dir()
APP_DIR = get_app_dir()
ENV_FILE = os.path.join(DATA_DIR, ".env")


def load_env_file():
    """加载 DATA_DIR/.env，并回填被容器环境变量置空的键。

    python-dotenv 默认不覆盖已存在的环境变量，而 docker compose 里常见的
    `PIXIV_REFRESH_TOKEN: ""` 会让「设置页保存到 .env 的真实值」永远读不到，
    重启后表现为 token 凭空消失。这里把「环境变量为空」视为未设置。
    """
    try:
        from dotenv import load_dotenv, dotenv_values
    except ImportError:
        return
    load_dotenv(ENV_FILE)
    try:
        values = dotenv_values(ENV_FILE)
    except OSError:
        return
    for key, value in values.items():
        if value and not (os.environ.get(key) or "").strip():
            os.environ[key] = value


# 各模块（run.py / main.py / sync.py …）都依赖 .env 生效，统一在这里加载一次，
# 之后它们自己的 load_dotenv 调用变成无害的重复操作。
load_env_file()
