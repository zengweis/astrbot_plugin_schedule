"""课表的时间维度：节次作息、周次解析、日期展开、上课提醒计算。

**关于学期起始日**：正方教务对学生开放的接口不包含校历——
`rqazcList` / `djdzList` 恒为空，`/jwglxt/xtgl/index_cxXl*.html` 一类路径全部返回
「警告提示」页或 910，给课表接口追加 `zc` 参数也不会让日期字段变化（实测）。
因此「第 1 周周一」只能内置或由用户设置：见 `KNOWN_SEMESTER_STARTS` 与 `wakeup` 之外的
「设置开学」指令。缺少该值时本模块不做周次过滤，只按星期匹配，由图片自行标注周次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

try:
    from .schedule_parser import WEEKDAY_NAMES, Course
except ImportError:  # 插件以平铺模块方式加载时
    from schedule_parser import WEEKDAY_NAMES, Course

# 已知学期的「第 1 周周一」。键为 (xnm, xqm)，取值与课表页学年/学期下拉框一致。
# 数据来源：学校公开校历（教务系统不提供该信息）。
KNOWN_SEMESTER_STARTS: dict[tuple[str, str], str] = {
    ("2026", "3"): "2026-09-14",  # 2026-2027 第1学期：校历「9月14日开始上课」
    ("2026", "12"): "2027-03-01",  # 2026-2027 第2学期：校历「3月1日开始上课」
}

# 单次提醒最多提前多久，用于拦住明显不合理的输入
MAX_LEAD_MINUTES = 24 * 60


def local_now() -> datetime:
    """本地当前时间。

    显式带上时区而不是裸调 `datetime.now()`：一是让 `due_reminders` 里与
    「日期 + 节次时刻」拼出来的时间比较时两边都是 aware，二是便于测试时替换。
    """
    return datetime.now(timezone.utc).astimezone()


def local_today() -> date:
    return local_now().date()


@dataclass(slots=True)
class PeriodTime:
    """一个节次的起止时间。"""

    index: int
    start: time
    end: time
    day_part: str = ""


@dataclass(slots=True)
class Slot:
    """某一天里的一节课（一个节次区间）。"""

    course: Course
    start: time
    end: time
    first_period: int
    last_period: int

    @property
    def period_label(self) -> str:
        if self.first_period == self.last_period:
            return f"第{self.first_period}节"
        return f"第{self.first_period}-{self.last_period}节"

    @property
    def time_label(self) -> str:
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


@dataclass(slots=True)
class DayPlan:
    """一天的上课安排。"""

    day: date
    weekday: int
    is_today: bool
    week_no: int | None
    slots: list[Slot]

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.weekday - 1]

    @property
    def date_label(self) -> str:
        return f"{self.day.month}月{self.day.day}日"

    @property
    def week_label(self) -> str:
        return f"第{self.week_no}周" if self.week_no else ""


# --------------------------------------------------------------------- 节次作息


def _to_time(raw: str) -> time | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})", raw or "")
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def parse_periods(payload: object) -> list[PeriodTime]:
    """解析 `/jwglxt/kbcx/xskbcx_cxRjc.html` 返回的节次时间表。"""
    if not isinstance(payload, list):
        return []
    periods: list[PeriodTime] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            index = int(str(item.get("jcmc") or "").strip())
        except ValueError:
            continue
        start = _to_time(str(item.get("qssj") or ""))
        end = _to_time(str(item.get("jssj") or ""))
        if start is None or end is None:
            continue
        periods.append(
            PeriodTime(
                index=index, start=start, end=end, day_part=str(item.get("rsdmc") or "")
            )
        )
    periods.sort(key=lambda p: p.index)
    return periods


# --------------------------------------------------------------------- 周次


_WEEK_TOKEN = re.compile(r"(\d+)(?:\s*-\s*(\d+))?\s*周(?:\s*[（(]\s*(单|双)\s*[)）])?")


def parse_weeks(raw: str) -> set[int]:
    """把 `zcd` 解析成周次集合。

    支持教务系统实际出现的全部写法：
        1-3周,5-9周           -> {1,2,3,5..9}
        第18周                -> {18}
        1周,4-6周,8-15周,17周 -> {1,4,5,6,8..15,17}
        2周,5-17周(单)        -> {2} ∪ {5..17 中的奇数周}

    单/双周只作用于它紧跟的那个区间，这是教务系统的语义。
    """
    weeks: set[int] = set()
    if not raw:
        return weeks
    for match in _WEEK_TOKEN.finditer(raw):
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if last < first:
            first, last = last, first
        parity = match.group(3)
        for week in range(first, last + 1):
            if parity == "单" and week % 2 == 0:
                continue
            if parity == "双" and week % 2 == 1:
                continue
            weeks.add(week)
    return weeks


def parse_date_input(raw: str) -> date | None:
    """解析用户输入的日期，接受紧凑与带分隔符两种写法。

    为了少让用户查格式，下面这些都认：
        20260914 / 2026-09-14 / 2026/09/14 / 2026.9.14 / 2026年9月14日
    """
    text = (raw or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 8:
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    match = re.match(r"^(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def resolve_semester_start(
    year: str,
    term: str,
    override: str | None = None,
) -> date | None:
    """确定某学期的第一周周一。

    用户设置优先；未设置时回退到内置的校历表。两者都会归一到所在周的周一，
    因为用户可能输入报到日而非第一个上课日。
    """
    for candidate in (override, KNOWN_SEMESTER_STARTS.get((str(year), str(term)))):
        if not candidate:
            continue
        parsed = parse_date_input(str(candidate))
        if parsed is None:
            continue
        return parsed - timedelta(days=parsed.isoweekday() - 1)
    return None


def week_number(semester_start: date | None, day: date) -> int | None:
    """某天属于第几教学周；未知学期起始日时返回 None。"""
    if semester_start is None:
        return None
    offset = (day - semester_start).days
    if offset < 0:
        return None
    return offset // 7 + 1


# --------------------------------------------------------------------- 展开成日程


def _slot_times(
    periods: dict[int, PeriodTime], first: int, last: int
) -> tuple[time, time] | None:
    start = periods.get(first)
    end = periods.get(last) or start
    if start is None or end is None:
        return None
    return start.start, end.end


def plan_days(
    *,
    start_day: date,
    count: int,
    courses: list[Course],
    periods: list[PeriodTime],
    semester_start: date | None,
) -> list[DayPlan]:
    """把「每周重复 + 周次限定」的课表展开成连续若干天的具体安排。

    未提供学期起始日时不做周次过滤——宁可多显示并标注周次，也不要凭猜测漏掉课程。
    """
    period_map = {p.index: p for p in periods}
    plans: list[DayPlan] = []
    today = local_today()

    for offset in range(max(0, count)):
        day = start_day + timedelta(days=offset)
        weekday = day.isoweekday()
        week = week_number(semester_start, day)

        slots: list[Slot] = []
        for course in courses:
            if course.weekday != weekday:
                continue
            if week is not None:
                weeks = parse_weeks(course.weeks)
                if weeks and week not in weeks:
                    continue
            times = _slot_times(period_map, course.start_section, course.end_section)
            if times is None:
                continue
            slots.append(
                Slot(
                    course=course,
                    start=times[0],
                    end=times[1],
                    first_period=course.start_section,
                    last_period=course.end_section,
                )
            )
        slots.sort(key=lambda s: (s.start, s.course.name))
        plans.append(
            DayPlan(
                day=day,
                weekday=weekday,
                is_today=(day == today),
                week_no=week,
                slots=slots,
            )
        )
    return plans


# --------------------------------------------------------------------- 上课提醒


def reminder_key(day: date, slot: Slot) -> str:
    """同一节课只提醒一次的幂等键。"""
    return f"{day.isoformat()}|{slot.course.name}|{slot.period_label}|{slot.start.strftime('%H%M')}"


def due_reminders(
    *,
    now: datetime,
    courses: list[Course],
    periods: list[PeriodTime],
    semester_start: date | None,
    lead_minutes: int,
) -> list[tuple[date, Slot]]:
    """返回此刻应当提醒的课程。

    判据：`开始时间 - 提前量 <= now < 开始时间`。调用方负责用 `reminder_key` 去重。
    """
    if lead_minutes <= 0 or not periods:
        return []
    plan = plan_days(
        start_day=now.date(),
        count=1,
        courses=courses,
        periods=periods,
        semester_start=semester_start,
    )
    if not plan:
        return []

    due: list[tuple[date, Slot]] = []
    for slot in plan[0].slots:
        # 用 now 的 tzinfo 拼出上课时刻，保证两边同为 aware 或同为 naive 才能比较
        slot_start = datetime.combine(now.date(), slot.start, tzinfo=now.tzinfo)
        if slot_start - timedelta(minutes=lead_minutes) <= now < slot_start:
            due.append((now.date(), slot))
    return due
