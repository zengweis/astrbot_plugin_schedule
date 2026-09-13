"""AstrBot 课表插件 —— 浙大宁波理工学院教务系统个人课表导入。

指令：
    设置信息 <学号/账号> <密码>   保存教务系统登录凭据
    导入个人课表                  登录教务系统，抓取最新学期课表，格式化后落盘

鉴权链路为「WebVPN → 金智统一身份认证（CAS）→ 正方教务 jwglxt」，全部细节见 schedule_client.py。
本插件对教务系统只做读操作：登录、读取课表，不会修改课表、选课或任何账号数据。
"""

from __future__ import annotations

import traceback

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 插件被作为包加载（相对导入可用）
    from .schedule_client import (
        JwglxtAuthError,
        JwglxtCaptchaRequired,
        JwglxtClient,
        JwglxtError,
        JwglxtSessionExpired,
    )
    from .schedule_parser import render_summary
    from .schedule_store import CredentialStore, TimetableStore
except ImportError:  # 插件被作为平铺模块加载
    import sys as _sys
    from pathlib import Path as _Path

    _plugin_dir = str(_Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)

    from schedule_client import (  # type: ignore[no-redef]
        JwglxtAuthError,
        JwglxtCaptchaRequired,
        JwglxtClient,
        JwglxtError,
        JwglxtSessionExpired,
    )
    from schedule_parser import render_summary  # type: ignore[no-redef]
    from schedule_store import CredentialStore, TimetableStore  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_schedule"

CMD_SET_PROFILE = "设置信息"
CMD_IMPORT = "导入个人课表"

USAGE_SET = "用法：设置信息 <学号/账号> <密码>\n例如：设置信息 3260227024 yourpassword"


def _args_after_command(message: str, command: str) -> str:
    """取指令名之后的参数。

    不同平台的 `message_str` 有的含指令名、有的只含参数，因此以「找到指令名就切掉」为准，
    找不到则整串视为参数。
    """
    text = message or ""
    index = text.find(command)
    if index < 0:
        return text.strip()
    return text[index + len(command) :].strip()


def _is_private_chat(event: AstrMessageEvent) -> bool | None:
    """探测是否私聊。返回 None 表示当前 AstrBot 版本无法判定。"""
    probe = getattr(event, "is_private_chat", None)
    if not callable(probe):
        return None
    try:
        return bool(probe())
    except Exception:  # noqa: BLE001
        return None


@register(
    PLUGIN_NAME,
    "JERRY WEI",
    "浙大宁波理工学院教务系统个人课表导入：设置账号后一键抓取最新学期课表并格式化落盘。",
    "v1.0.0",
)
class SchedulePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.credentials = CredentialStore()
        self.timetables = TimetableStore()

    async def initialize(self) -> None:
        logger.info(f"[schedule] 插件已加载，数据目录：{self.timetables.dir}")

    @filter.command(CMD_SET_PROFILE)
    async def cmd_set_profile(self, event: AstrMessageEvent):
        """设置教务系统账号密码：设置信息 <学号/账号> <密码>"""
        args = _args_after_command(event.message_str, CMD_SET_PROFILE)

        if not args:
            current = self.credentials.load()
            if current:
                state = f"当前已保存：{current.masked}（更新于 {current.updated_at}）"
            else:
                state = "当前还没有保存账号信息。"
            yield event.plain_result(
                f"{state}\n\n{USAGE_SET}\n\n"
                "说明：统一身份认证没有长期令牌，抓取课表时需要用口令重新登录，"
                "因此密码会明文保存在 AstrBot 的插件数据目录，建议在私聊中设置。"
            )
            return

        parts = args.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result(f"参数不完整。\n{USAGE_SET}")
            return

        username, password = parts[0].strip(), parts[1].strip()
        if not username or not password:
            yield event.plain_result(f"账号和密码都不能为空。\n{USAGE_SET}")
            return

        if _is_private_chat(event) is False:
            yield event.plain_result(
                "为避免口令在群聊中泄露，请在与机器人的私聊里发送「设置信息」。"
            )
            return

        try:
            saved = self.credentials.save(username, password)
        except OSError as exc:
            logger.error(f"[schedule] 保存凭据失败: {exc}")
            yield event.plain_result(f"保存失败，无法写入数据目录：{exc}")
            return

        yield event.plain_result(
            f"已保存账号信息：{saved.masked}\n"
            f"记录时间：{saved.updated_at}\n\n"
            "接下来可以发送「导入个人课表」。"
        )

    @filter.command(CMD_IMPORT)
    async def cmd_import_timetable(self, event: AstrMessageEvent):
        """登录教务系统，抓取最新学期个人课表并保存到本地文件。"""
        credentials = self.credentials.load()
        if credentials is None:
            yield event.plain_result(f"还没有保存账号信息，请先发送：\n{USAGE_SET}")
            return

        yield event.plain_result("正在登录教务系统并抓取课表，请稍候……")

        try:
            async with JwglxtClient(
                credentials.username, credentials.password
            ) as client:
                timetable = await client.import_timetable()
        except JwglxtCaptchaRequired as exc:
            yield event.plain_result(f"登录被拦截：{exc}")
            return
        except JwglxtAuthError as exc:
            yield event.plain_result(
                f"登录失败：{exc}\n请用「设置信息」重新核对账号密码，确认账号已激活。"
            )
            return
        except JwglxtSessionExpired as exc:
            yield event.plain_result(f"抓取失败：{exc}\n稍后再试一次通常就能恢复。")
            return
        except JwglxtError as exc:
            yield event.plain_result(f"抓取失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 — 插件不能让单个异常把整个机器人带崩
            logger.error(
                f"[schedule] 导入课表时出现未预期异常：\n{traceback.format_exc()}"
            )
            yield event.plain_result(f"抓取课表时出现未预期错误：{exc}")
            return

        try:
            paths = self.timetables.save(timetable)
        except OSError as exc:
            logger.error(f"[schedule] 写入课表文件失败: {exc}")
            yield event.plain_result(
                f"课表已抓取成功，但写入本地文件失败：{exc}\n"
                f"数据目录：{self.timetables.dir}"
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[schedule] 生成课表文件时出现未预期异常：\n{traceback.format_exc()}"
            )
            yield event.plain_result(f"课表已抓取成功，但生成文件时出错：{exc}")
            return

        saved_list = "\n".join(f"  · {path}" for path in paths)
        yield event.plain_result(
            f"{render_summary(timetable)}\n\n已保存到本地文件：\n{saved_list}"
        )

    async def terminate(self) -> None:
        logger.info("[schedule] 插件已卸载/停用。")
