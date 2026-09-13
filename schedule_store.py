"""持久化：账号凭据与课表文件。

全部落在 AstrBot 的插件数据目录 `data/plugin_data/astrbot_plugin_schedule/`，
**不写插件自身目录**——插件更新或重装会覆盖源码目录，数据必须放在 data 下。

安全说明：统一身份认证没有长期令牌，每次抓取都要用明文口令走一次 AES 登录，
因此口令必须以可逆形式落盘。本模块把凭据文件权限收紧到 0600（POSIX），
并在文件与日志中不做任何额外扩散；但请明确知悉：**同机上的其他进程仍可读取该文件**。
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .schedule_parser import Timetable, render_markdown
except ImportError:  # 插件以平铺模块方式加载时
    from schedule_parser import Timetable, render_markdown

PLUGIN_NAME = "astrbot_plugin_schedule"
CREDENTIAL_FILE = "credentials.json"
TIMETABLE_JSON = "timetable.json"
TIMETABLE_MD = "timetable.md"

_data_dir_cache: Path | None = None


def data_dir() -> Path:
    """返回插件数据目录，优先走 AstrBot 官方接口，失败则按标准目录结构回退。"""
    global _data_dir_cache
    if _data_dir_cache is not None and _data_dir_cache.exists():
        return _data_dir_cache

    try:
        from astrbot.api.star import StarTools

        resolved = Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001 — AstrBot 未就绪或版本差异时不能中断插件
        # 标准布局：<AstrBot>/data/plugins/<plugin>/ → <AstrBot>/data/plugin_data/<plugin>/
        here = Path(__file__).resolve().parent
        resolved = here.parent.parent / "plugin_data" / PLUGIN_NAME

    resolved.mkdir(parents=True, exist_ok=True)
    _data_dir_cache = resolved
    return resolved


def _restrict(path: Path) -> None:
    """把文件权限收紧为仅属主可读写；Windows 上无 POSIX 权限位，静默跳过。"""
    if os.name != "posix":
        return
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再替换，避免写入中途被打断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, path)


@dataclass(slots=True)
class Credentials:
    username: str
    password: str
    updated_at: str = ""

    @property
    def masked(self) -> str:
        return f"{self.username} / {'*' * len(self.password)}"


class CredentialStore:
    """账号凭据的读写。"""

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or data_dir()) / CREDENTIAL_FILE

    @property
    def path(self) -> Path:
        return self._path

    def save(self, username: str, password: str) -> Credentials:
        creds = Credentials(
            username=username.strip(),
            password=password,
            updated_at=datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
        )
        _atomic_write(
            self._path, json.dumps(asdict(creds), ensure_ascii=False, indent=2)
        )
        return creds

    def load(self) -> Credentials | None:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        username = str(raw.get("username") or "").strip()
        password = str(raw.get("password") or "")
        if not username or not password:
            return None
        return Credentials(username, password, str(raw.get("updated_at") or ""))

    def clear(self) -> bool:
        if not self._path.exists():
            return False
        self._path.unlink()
        return True


class TimetableStore:
    """课表落盘：一份机器可读 JSON，一份人类可读 Markdown。"""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or data_dir()

    @property
    def dir(self) -> Path:
        return self._dir

    def save(self, timetable: Timetable) -> list[Path]:
        payload: dict[str, Any] = timetable.to_dict()
        payload["source"] = "nbt.edu.cn/jwglxt"
        _atomic_write(
            self._dir / TIMETABLE_JSON,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

        markdown = render_markdown(timetable)
        _atomic_write(self._dir / TIMETABLE_MD, markdown)

        # 另存一份按学期命名的快照，便于保留历史学期
        snapshot = self._dir / f"timetable_{timetable.slug}.json"
        _atomic_write(snapshot, json.dumps(payload, ensure_ascii=False, indent=2))

        return [self._dir / TIMETABLE_JSON, self._dir / TIMETABLE_MD, snapshot]

    def load(self) -> dict[str, Any] | None:
        path = self._dir / TIMETABLE_JSON
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
