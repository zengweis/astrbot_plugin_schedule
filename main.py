"""AstrBot 课表插件 —— 浙大宁波理工学院教务系统个人课表导入与提醒。

指令：
    设置信息 <学号/账号> <密码>   保存教务系统登录凭据
    导入个人课表                  登录教务系统，抓取最新学期课表，格式化后落盘
    我的课表 [天数]               渲染最近 N 天（含今天）的课程为图片，默认 7 天
    设置开学 <YYYYMMDD>           指定本学期第一周周一的日期，用于按周次过滤
    wakeup <提前分钟>             设定每节课提前多少分钟提醒；0 表示关闭

鉴权链路为「WebVPN → 金智统一身份认证（CAS）→ 正方教务 jwglxt」，细节见 schedule_client.py。
本插件对教务系统只做读操作：登录、读取课表，不会修改课表、选课或任何账号数据。
"""

from __future__ import annotations

import asyncio
import traceback
from datetime import date

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:  # 插件被作为包加载（相对导入可用）
    from .schedule_calendar import (
        KNOWN_SEMESTER_STARTS,
        MAX_LEAD_MINUTES,
        due_reminders,
        local_now,
        local_today,
        parse_date_input,
        plan_days,
        reminder_key,
        resolve_semester_start,
        week_number,
    )
    from .schedule_client import (
        JwglxtAuthError,
        JwglxtCaptchaRequired,
        JwglxtClient,
        JwglxtError,
        JwglxtSessionExpired,
    )
    from .schedule_image import (
        AgendaProfile,
        ScheduleImageError,
        fonts_available,
        save_agenda,
    )
    from .schedule_parser import render_summary
    from .schedule_store import CredentialStore, SettingsStore, TimetableStore
except ImportError:  # 插件被作为平铺模块加载
    import sys as _sys
    from pathlib import Path as _Path

    _plugin_dir = str(_Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)

    from schedule_calendar import (  # type: ignore[no-redef]
        KNOWN_SEMESTER_STARTS,
        MAX_LEAD_MINUTES,
        due_reminders,
        local_now,
        local_today,
        parse_date_input,
        plan_days,
        reminder_key,
        resolve_semester_start,
        week_number,
    )
    from schedule_client import (  # type: ignore[no-redef]
        JwglxtAuthError,
        JwglxtCaptchaRequired,
        JwglxtClient,
        JwglxtError,
        JwglxtSessionExpired,
    )
    from schedule_image import (  # type: ignore[no-redef]
        AgendaProfile,
        ScheduleImageError,
        fonts_available,
        save_agenda,
    )
    from schedule_parser import render_summary  # type: ignore[no-redef]
    from schedule_store import (  # type: ignore[no-redef]
        CredentialStore,
        SettingsStore,
        TimetableStore,
    )

PLUGIN_NAME = "astrbot_plugin_schedule"

CMD_SET_PROFILE = "设置信息"
CMD_IMPORT = "导入个人课表"
CMD_MY_SCHEDULE = "我的课表"
CMD_SET_TERM_START = "设置开学"
CMD_WAKEUP = "wakeup"

DEFAULT_DAYS = 7
MAX_DAYS = 60
REMINDER_TICK_SECONDS = 30

USAGE_SET_PROFILE = (
    "用法：设置信息 <学号/账号> <密码>\n例如：设置信息 3260227024 yourpassword"
)
USAGE_MY_SCHEDULE = "用法：我的课表 [天数]\n例如：我的课表 7（查看最近 7 天，含今天）"
USAGE_SET_TERM_START = (
    "用法：设置开学 <第一周周一的日期>\n"
    "例如：设置开学 20260914（2026 年 9 月 14 日）\n"
    "也可以写成 2026-09-14，插件会自动归一到那一周的周一。"
)
USAGE_WAKEUP = (
    "用法：wakeup <提前分钟>\n例如：wakeup 15（每节课提前 15 分钟提醒）；wakeup 0 关闭"
)


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


def _session_of(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")


@register(
    PLUGIN_NAME,
    "JERRY WEI",
    "浙大宁波理工学院教务系统个人课表：设置账号后导入课表、渲染课表图片、上课前提醒。",
    "v0.2",
)
class SchedulePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.credentials = CredentialStore()
        self.timetables = TimetableStore()
        self.settings = SettingsStore()
        self._reminder_task: asyncio.Task[None] | None = None
        self._fired: set[str] = set()
        self._fired_day: date | None = None

    async def initialize(self) -> None:
        logger.info(f"[schedule] 插件已加载，数据目录：{self.timetables.dir}")
        if not fonts_available():
            logger.warning(
                "[schedule] 未检测到中文字体，「我的课表」将无法渲染图片。"
                "Debian/Ubuntu 可执行 apt-get install -y fonts-noto-cjk"
            )
        self._reminder_task = asyncio.create_task(self._reminder_loop())

    # ------------------------------------------------------------ 后台提醒

    async def _reminder_loop(self) -> None:
        """每 30 秒检查一次是否有课即将开始。

        循环内部把所有异常都吃掉并记日志——后台任务崩溃不应该影响机器人本体。
        """
        while True:
            try:
                await asyncio.sleep(REMINDER_TICK_SECONDS)
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.error(f"[schedule] 提醒任务异常：\n{traceback.format_exc()}")

    async def _tick(self) -> None:
        settings = self.settings.load()
        if not settings.wakeup_enabled or not settings.notify_sessions:
            return

        timetable = self.timetables.load_timetable()
        periods = self.timetables.load_periods()
        if timetable is None or not periods:
            return

        now = local_now()
        if self._fired_day != now.date():
            self._fired.clear()
            self._fired_day = now.date()

        semester_start = resolve_semester_start(
            timetable.year, timetable.term, settings.semester_start
        )
        due = due_reminders(
            now=now,
            courses=timetable.courses,
            periods=periods,
            semester_start=semester_start,
            lead_minutes=settings.wakeup_minutes,
        )
        for day, slot in due:
            key = reminder_key(day, slot)
            if key in self._fired:
                continue
            self._fired.add(key)
            await self._push_reminder(
                settings.notify_sessions, settings.wakeup_minutes, slot
            )

    async def _push_reminder(self, sessions: list[str], lead: int, slot) -> None:
        course = slot.course
        parts = [
            f"{lead} 分钟后上课",
            course.name,
            f"{slot.time_label} · {slot.period_label}",
        ]
        if course.place:
            parts.append(f"地点：{course.place}")
        if course.teacher:
            parts.append(f"教师：{course.teacher}")
        text = "\n".join(parts)

        for session in sessions:
            try:
                await self.context.send_message(session, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001 — 单会话失败不影响其他会话
                logger.error(f"[schedule] 推送提醒到 {session} 失败：{exc}")

    # ------------------------------------------------------------ 凭据

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
                f"{state}\n\n{USAGE_SET_PROFILE}\n\n"
                "说明：统一身份认证没有长期令牌，抓取课表时需要用口令重新登录，"
                "因此密码会明文保存在 AstrBot 的插件数据目录，建议在私聊中设置。"
            )
            return

        parts = args.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            yield event.plain_result(f"参数不完整。\n{USAGE_SET_PROFILE}")
            return

        username, password = parts[0].strip(), parts[1].strip()
        if not username or not password:
            yield event.plain_result(f"账号和密码都不能为空。\n{USAGE_SET_PROFILE}")
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

    # ------------------------------------------------------------ 导入

    @filter.command(CMD_IMPORT)
    async def cmd_import_timetable(self, event: AstrMessageEvent):
        """登录教务系统，抓取最新学期个人课表并保存到本地文件。"""
        credentials = self.credentials.load()
        if credentials is None:
            yield event.plain_result(
                f"还没有保存账号信息，请先发送：\n{USAGE_SET_PROFILE}"
            )
            return

        yield event.plain_result("正在登录教务系统并抓取课表，请稍候……")

        try:
            async with JwglxtClient(
                credentials.username, credentials.password
            ) as client:
                timetable = await client.import_timetable()
                periods = await client.fetch_periods(timetable.year, timetable.term)
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
            if periods:
                self.timetables.save_periods(periods)
        except OSError as exc:
            logger.error(f"[schedule] 写入课表文件失败: {exc}")
            yield event.plain_result(
                f"课表已抓取成功，但写入本地文件失败：{exc}\n数据目录：{self.timetables.dir}"
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[schedule] 生成课表文件时出现未预期异常：\n{traceback.format_exc()}"
            )
            yield event.plain_result(f"课表已抓取成功，但生成文件时出错：{exc}")
            return

        saved_list = "\n".join(f"  · {path}" for path in paths)
        period_note = (
            f"\n节次作息：已记录 {len(periods)} 个节次"
            if periods
            else "\n未取到节次作息表，稍后可用「导入个人课表」重试补齐"
        )
        yield event.plain_result(
            f"{render_summary(timetable)}\n\n已保存到本地文件：\n{saved_list}{period_note}"
        )

    # ------------------------------------------------------------ 开学日期

    @filter.command(CMD_SET_TERM_START)
    async def cmd_set_term_start(self, event: AstrMessageEvent):
        """指定本学期第一周周一的日期：设置开学 20260914"""
        args = _args_after_command(event.message_str, CMD_SET_TERM_START)

        suggestion = ""
        timetable = self.timetables.load_timetable()
        if timetable is not None:
            builtin = KNOWN_SEMESTER_STARTS.get((timetable.year, timetable.term))
            if builtin:
                suggestion = (
                    f"\n校历记载本学期第一周周一为 {builtin.replace('-', '')}。"
                )

        if not args:
            settings = self.settings.load()
            current = (
                f"当前已设置：{settings.semester_start}"
                if settings.semester_start
                else "当前还没有设置开学日期。"
            )
            yield event.plain_result(f"{current}{suggestion}\n\n{USAGE_SET_TERM_START}")
            return

        parsed = parse_date_input(args)
        if parsed is None:
            yield event.plain_result(
                f"没看懂这个日期：{args}\n\n{USAGE_SET_TERM_START}"
            )
            return

        # 归一化到周一：用户输入的可能是报到日而不是第一个上课日
        monday = parsed.fromordinal(parsed.toordinal() - (parsed.isoweekday() - 1))
        try:
            self.settings.save_semester_start(args.strip())
        except OSError as exc:
            yield event.plain_result(f"保存失败，无法写入数据目录：{exc}")
            return

        today_week = week_number(monday, local_today())
        note = (
            f"今天是第 {today_week} 周。" if today_week else "今天在该学期起始日之前。"
        )
        adjusted = (
            f"\n（你输入的是 {parsed.isoformat()}，已归一到所在周的周一 {monday.isoformat()}）"
            if monday != parsed
            else ""
        )
        yield event.plain_result(
            f"已设置第一周周一：{monday.isoformat()}。{adjusted}\n{note}\n"
            "之后「我的课表」会按这个日期过滤周次。"
        )

    # ------------------------------------------------------------ 我的课表

    @filter.command(CMD_MY_SCHEDULE)
    async def cmd_my_schedule(self, event: AstrMessageEvent):
        """渲染最近 N 天（含今天）的课程为图片，默认 7 天。"""
        args = _args_after_command(event.message_str, CMD_MY_SCHEDULE)
        days = DEFAULT_DAYS
        if args:
            raw = "".join(ch for ch in args.split()[0] if ch.isdigit())
            if not raw:
                yield event.plain_result(f"天数需要是数字。\n{USAGE_MY_SCHEDULE}")
                return
            days = max(1, min(MAX_DAYS, int(raw)))

        timetable = self.timetables.load_timetable()
        if timetable is None:
            yield event.plain_result(
                "本地还没有课表数据。\n请先发送「设置信息」保存账号，再发送「导入个人课表」。"
            )
            return

        periods = self.timetables.load_periods()
        if not periods:
            yield event.plain_result(
                "缺少节次作息表，无法把「第几节」换算成具体时间。\n"
                "请重新发送「导入个人课表」补齐。"
            )
            return

        settings = self.settings.load()
        semester_start = resolve_semester_start(
            timetable.year, timetable.term, settings.semester_start
        )

        try:
            plans = plan_days(
                start_day=local_today(),
                count=days,
                courses=timetable.courses,
                periods=periods,
                semester_start=semester_start,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[schedule] 展开日程失败：\n{traceback.format_exc()}")
            yield event.plain_result(f"整理课表时出错：{exc}")
            return

        profile = AgendaProfile(
            name=timetable.student_name,
            student_id=timetable.student_id,
            class_name=timetable.class_name,
            semester_label=timetable.semester_label,
            days=days,
            semester_start=settings.semester_start,
            week_now=week_number(semester_start, local_today()),
        )

        try:
            image_path = save_agenda(plans, profile, self.timetables.dir)
        except ScheduleImageError as exc:
            yield event.plain_result(f"渲染课表图片失败：{exc}")
            return
        except OSError as exc:
            logger.error(f"[schedule] 写入课表图片失败：{exc}")
            yield event.plain_result(f"渲染课表图片失败，无法写入数据目录：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error(
                f"[schedule] 渲染课表图片时出现未预期异常：\n{traceback.format_exc()}"
            )
            yield event.plain_result(f"渲染课表图片时出现未预期错误：{exc}")
            return

        total = sum(len(plan.slots) for plan in plans)
        busy_days = sum(1 for plan in plans if plan.slots)
        lines = [f"最近 {days} 天（含今天）共 {total} 节课，分布在 {busy_days} 天。"]
        if semester_start is None:
            builtin = KNOWN_SEMESTER_STARTS.get((timetable.year, timetable.term))
            hint = (
                f"例如：设置开学 {builtin.replace('-', '')}"
                if builtin
                else USAGE_SET_TERM_START
            )
            lines.append(
                "提示：尚未设置开学日期，图片未按周次过滤，可能列出本周并不上的课。\n"
                + hint
            )
        yield event.plain_result("\n".join(lines))
        yield event.image_result(str(image_path))

    # ------------------------------------------------------------ 上课提醒

    @filter.command(CMD_WAKEUP)
    async def cmd_wakeup(self, event: AstrMessageEvent):
        """设置每节课提前多少分钟提醒：wakeup <提前分钟>"""
        args = _args_after_command(event.message_str, CMD_WAKEUP)
        settings = self.settings.load()

        if not args:
            if settings.wakeup_enabled:
                state = f"当前设定：每节课提前 {settings.wakeup_minutes} 分钟提醒。"
            else:
                state = "当前未开启上课提醒。"
            yield event.plain_result(f"{state}\n\n{USAGE_WAKEUP}")
            return

        head = args.split()[0]
        if head.lower() in {"off", "关闭", "取消"}:
            minutes = 0
        else:
            raw = "".join(ch for ch in head if ch.isdigit())
            if not raw:
                yield event.plain_result(f"提前量需要是数字（分钟）。\n{USAGE_WAKEUP}")
                return
            minutes = int(raw)
            if minutes < 0 or minutes > MAX_LEAD_MINUTES:
                yield event.plain_result(
                    f"提前量需要在 0 到 {MAX_LEAD_MINUTES} 分钟之间。\n{USAGE_WAKEUP}"
                )
                return

        try:
            saved = self.settings.save_wakeup(minutes, _session_of(event))
        except OSError as exc:
            yield event.plain_result(f"保存失败，无法写入数据目录：{exc}")
            return

        if not saved.wakeup_enabled:
            yield event.plain_result("已关闭上课提醒。")
            return

        notes = [f"已设定：每节课提前 {saved.wakeup_minutes} 分钟提醒。"]
        if not self.timetables.load_timetable():
            notes.append("注意：本地还没有课表数据，请先发送「导入个人课表」。")
        if not self.timetables.load_periods():
            notes.append("注意：缺少节次作息表，请重新发送一次「导入个人课表」。")
        notes.append(f"提醒会推送到已登记的 {len(saved.notify_sessions)} 个会话。")
        yield event.plain_result("\n".join(notes))

    async def terminate(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — 卸载阶段不应再抛异常
                logger.warning(f"[schedule] 停止提醒任务时出错：{exc}")
            self._reminder_task = None
        logger.info("[schedule] 插件已卸载/停用。")
