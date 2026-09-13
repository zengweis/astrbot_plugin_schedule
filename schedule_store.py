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
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

try:
    from .schedule_calendar import PeriodTime
    from .schedule_parser import Timetable, render_markdown, timetable_from_dict
except ImportError:  # 插件以平铺模块方式加载时
    from schedule_calendar import PeriodTime
    from schedule_parser import Timetable, render_markdown, timetable_from_dict

PLUGIN_NAME = "astrbot_plugin_schedule"
CREDENTIAL_FILE = "credentials.json"
SETTINGS_FILE = "settings.json"
TIMETABLE_JSON = "timetable.json"
TIMETABLE_MD = "timetable.md"
PERIODS_JSON = "periods.json"

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


@dataclass(slots=True)
class Settings:
    """插件运行参数。"""

    wakeup_minutes: int = 0
    semester_start: str = ""
    notify_sessions: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def wakeup_enabled(self) -> bool:
        return self.wakeup_minutes > 0


class SettingsStore:
    """`wakeup` 提前量与开学日期等运行参数的读写。

    `notify_sessions` 记录发起过 `wakeup` 的会话（unified_msg_origin），
    提醒任务据此把消息推回原会话。
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or data_dir()) / SETTINGS_FILE

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Settings:
        if not self._path.exists():
            return Settings()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Settings()
        if not isinstance(raw, dict):
            return Settings()
        try:
            minutes = int(raw.get("wakeup_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        sessions = raw.get("notify_sessions") or []
        return Settings(
            wakeup_minutes=max(0, minutes),
            semester_start=str(raw.get("semester_start") or ""),
            notify_sessions=[str(s) for s in sessions if s],
            updated_at=str(raw.get("updated_at") or ""),
        )

    def _write(self, settings: Settings) -> Settings:
        settings.updated_at = (
            datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        )
        _atomic_write(
            self._path, json.dumps(asdict(settings), ensure_ascii=False, indent=2)
        )
        return settings

    def save_wakeup(self, minutes: int, session: str = "") -> Settings:
        settings = self.load()
        settings.wakeup_minutes = max(0, int(minutes))
        if session and session not in settings.notify_sessions:
            settings.notify_sessions.append(session)
        return self._write(settings)

    def save_semester_start(self, raw: str) -> Settings:
        settings = self.load()
        settings.semester_start = str(raw or "").strip()
        return self._write(settings)


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

    def load_timetable(self) -> Timetable | None:
        """读回结构化课表；没有导入过或文件损坏时返回 None。"""
        data = self.load()
        if not data:
            return None
        try:
            timetable = timetable_from_dict(data)
        except (TypeError, ValueError):
            return None
        return timetable if timetable.courses else None

    def save_periods(self, periods: list[PeriodTime]) -> None:
        """节次作息表独立落盘——它只随学期变化，不必每次导入都重新抓。

        `time` 不能直接被 json 序列化，统一存成 ISO 字符串。
        """
        payload = [
            {
                "index": p.index,
                "start": p.start.strftime("%H:%M"),
                "end": p.end.strftime("%H:%M"),
                "day_part": p.day_part,
            }
            for p in periods
        ]
        _atomic_write(
            self._dir / PERIODS_JSON,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )

    def load_periods(self) -> list[PeriodTime]:
        path = self._dir / PERIODS_JSON
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        periods: list[PeriodTime] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                periods.append(
                    PeriodTime(
                        index=int(item["index"]),
                        start=time.fromisoformat(str(item["start"])),
                        end=time.fromisoformat(str(item["end"])),
                        day_part=str(item.get("day_part") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        periods.sort(key=lambda p: p.index)
        return periods
