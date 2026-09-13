"""课表数据模型、解析与格式化。

数据源：正方教务 jwglxt `/jwglxt/kbcx/xskbcx_cxXsgrkb.html` 返回的 JSON。
该接口在 `kbList` 中给出逐条排课记录，字段为教务系统内部命名（`kcmc` / `xqj` / `jcs` …），
本模块负责把内部命名翻译成稳定、语义清晰的模型，并产出可直接落盘的文本。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

WEEKDAY_NAMES = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


@dataclass(slots=True)
class Course:
    """一条排课记录（同一天同一时段的一门课可能因周次不同而拆成多条）。"""

    name: str
    teacher: str
    weekday: int
    weekday_name: str
    sections: str
    start_section: int
    end_section: int
    weeks: str
    room: str
    building: str
    credits: str
    code: str
    category: str
    kind: str

    @property
    def place(self) -> str:
        """上课地点。教务系统的 `cdmc` 通常已含楼号前缀，避免与 `lh` 拼出 `SASA202-1` 这类重复。"""
        room = self.room.strip()
        building = self.building.strip()
        if not building or room.startswith(building):
            return room
        return f"{building}{room}"


@dataclass(slots=True)
class Timetable:
    """一个学期完整的个人课表。"""

    student_name: str = ""
    student_id: str = ""
    class_name: str = ""
    major: str = ""
    college: str = ""
    grade: str = ""
    year: str = ""
    year_name: str = ""
    term: str = ""
    term_name: str = ""
    fetched_at: str = ""
    courses: list[Course] = field(default_factory=list)

    @property
    def semester_label(self) -> str:
        year = self.year_name or self.year
        if not year:
            return ""
        return f"{year} 学年 第{self.term_name or self.term}学期"

    @property
    def slug(self) -> str:
        """用于文件名的稳定标识，例如 `2026-2027-1`。"""
        year = (self.year_name or self.year or "unknown").replace("/", "-")
        term = self.term_name or self.term or "0"
        return f"{year}-{term}"

    @property
    def course_count(self) -> int:
        return len({c.name for c in self.courses})

    def to_dict(self) -> dict[str, Any]:
        return {
            "student_name": self.student_name,
            "student_id": self.student_id,
            "class_name": self.class_name,
            "major": self.major,
            "college": self.college,
            "grade": self.grade,
            "year": self.year,
            "year_name": self.year_name,
            "term": self.term,
            "term_name": self.term_name,
            "semester_label": self.semester_label,
            "fetched_at": self.fetched_at,
            "courses": [asdict(c) for c in self.courses],
        }


def _digits(raw: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", raw or "")]


def parse_sections(raw: str) -> tuple[int, int]:
    """把 `1-4` / `3` / `1-4节` 统一成 (起始节, 结束节)。"""
    nums = _digits(raw)
    if not nums:
        return (0, 0)
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (nums[0], nums[1])


def parse_timetable(payload: dict[str, Any]) -> Timetable:
    """把教务系统原始 JSON 翻译成 `Timetable`。

    缺少 `kbList` 时返回空课表而不是抛错——某些学期确实会查不到数据，
    这属于正常业务状态，调用方应据此提示用户而不是报错。
    """
    info = payload.get("xsxx") or {}
    timetable = Timetable(
        student_name=str(info.get("XM") or ""),
        student_id=str(info.get("XH") or ""),
        class_name=str(info.get("BJMC") or ""),
        major=str(info.get("ZYMC") or ""),
        grade=str(info.get("NJDM_ID") or ""),
        year=str(info.get("XNM") or ""),
        year_name=str(info.get("XNMC") or ""),
        term=str(info.get("XQM") or ""),
        term_name=str(info.get("XQMMC") or ""),
        fetched_at=datetime.now(timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S"),
    )

    for item in payload.get("kbList") or []:
        if not isinstance(item, dict):
            continue
        raw_weekday = str(item.get("xqj") or "").strip()
        try:
            weekday = int(raw_weekday)
        except ValueError:
            continue
        if not 1 <= weekday <= 7:
            continue

        section_raw = str(item.get("jcs") or item.get("jcor") or item.get("jc") or "")
        start, end = parse_sections(section_raw)
        if start <= 0:
            continue

        timetable.courses.append(
            Course(
                name=str(item.get("kcmc") or "未知课程"),
                teacher=str(item.get("xm") or ""),
                weekday=weekday,
                weekday_name=str(item.get("xqjmc") or WEEKDAY_NAMES[weekday - 1]),
                sections=f"{start}-{end}" if start != end else str(start),
                start_section=start,
                end_section=end,
                weeks=str(item.get("zcd") or ""),
                room=str(item.get("cdmc") or ""),
                building=str(item.get("lh") or ""),
                credits=str(item.get("xf") or ""),
                code=str(item.get("kch") or ""),
                category=str(item.get("kclbmc") or item.get("kclb") or ""),
                kind=str(item.get("kcbj") or ""),
            )
        )

    timetable.courses.sort(key=lambda c: (c.weekday, c.start_section, c.name))
    return timetable


def _cell_safe(text: str) -> str:
    """转义会破坏 Markdown 表格的字符。"""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_weekly_markdown(timetable: Timetable) -> str:
    """渲染周视图表格，跨节次的连续课程用 `〃` 表示延续。"""
    courses = timetable.courses
    if not courses:
        return "（本学期暂无排课数据）"

    max_section = max(c.end_section for c in courses)
    # (weekday, section) -> 覆盖到该格的课程列表
    grid: dict[tuple[int, int], list[Course]] = {}
    for course in courses:
        for section in range(course.start_section, course.end_section + 1):
            grid.setdefault((course.weekday, section), []).append(course)

    used_weekdays = sorted({c.weekday for c in courses})
    header = (
        "| 节次 | " + " | ".join(WEEKDAY_NAMES[d - 1] for d in used_weekdays) + " |"
    )
    divider = "| --- |" + " --- |" * len(used_weekdays)
    lines = [header, divider]

    for section in range(1, max_section + 1):
        cells: list[str] = []
        for weekday in used_weekdays:
            entries = grid.get((weekday, section)) or []
            if not entries:
                cells.append("")
                continue
            chunks: list[str] = []
            for course in sorted(entries, key=lambda c: c.name):
                if section == course.start_section:
                    detail = course.name
                    if course.place:
                        detail += f"<br>{course.place}"
                    if course.weeks:
                        detail += f"<br>{course.weeks}"
                    chunks.append(detail)
                else:
                    chunks.append("〃")
            cells.append("<br>".join(chunks))
        lines.append(f"| {section} | " + " | ".join(cells) + " |")

    return "\n".join(lines)


def render_course_table(timetable: Timetable) -> str:
    lines = [
        "| 星期 | 节次 | 课程 | 教师 | 地点 | 周次 | 学分 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in timetable.courses:
        lines.append(
            f"| {_cell_safe(c.weekday_name)} | {_cell_safe(c.sections)} | "
            f"{_cell_safe(c.name)} | {_cell_safe(c.teacher)} | {_cell_safe(c.place)} | "
            f"{_cell_safe(c.weeks)} | {_cell_safe(c.credits)} |"
        )
    return "\n".join(lines)


def render_markdown(timetable: Timetable) -> str:
    """产出完整可读的 Markdown 课表（落盘用）。"""
    meta = [
        f"- 姓名：{timetable.student_name}",
        f"- 学号：{timetable.student_id}",
        f"- 班级：{timetable.class_name}",
        f"- 专业：{timetable.major}",
        f"- 学年学期：{timetable.semester_label}",
        f"- 课程数：{timetable.course_count}",
        f"- 抓取时间：{timetable.fetched_at}",
    ]
    return "\n".join(
        [
            "# 个人课表",
            "",
            *meta,
            "",
            "## 周视图",
            "",
            render_weekly_markdown(timetable),
            "",
            "## 课程明细",
            "",
            render_course_table(timetable),
            "",
            f"> 数据来源：浙大宁波理工学院教务系统（正方 jwglxt），经 WebVPN 抓取于 {timetable.fetched_at}。",
            "",
        ]
    )


def render_summary(timetable: Timetable, *, max_courses: int = 40) -> str:
    """产出适合聊天窗口展示的紧凑摘要（按星期分组）。"""
    lines = [
        "课表导入成功",
        f"  姓名：{timetable.student_name}（{timetable.student_id}）",
        f"  班级：{timetable.class_name}",
        f"  学期：{timetable.semester_label}",
        f"  课程：{timetable.course_count} 门 / {len(timetable.courses)} 条安排",
        "",
    ]
    if not timetable.courses:
        lines.append("本学期的课表还是空的，可能还没开放选课结果。")
        return "\n".join(lines)

    shown = 0
    for weekday in range(1, 8):
        day_courses = [c for c in timetable.courses if c.weekday == weekday]
        if not day_courses:
            continue
        lines.append(f"{WEEKDAY_NAMES[weekday - 1]}")
        for c in day_courses:
            if shown >= max_courses:
                lines.append("  ……（还有更多，见本地文件）")
                return "\n".join(lines)
            place = f" @{c.place}" if c.place else ""
            weeks = f" [{c.weeks}]" if c.weeks else ""
            lines.append(f"  第{c.sections}节 {c.name}{place}{weeks}")
            shown += 1
        lines.append("")
    return "\n".join(lines).rstrip()
