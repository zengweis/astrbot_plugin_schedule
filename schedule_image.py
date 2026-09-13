"""用 Pillow 把日程渲染成课表图片。

设计取向：浅色扁平、无阴影、小圆角，信息密度优先——一眼能看完最近几天上什么课。
字体依赖系统 CJK 字体，找不到时抛 `ScheduleImageError` 并给出安装指引，
而不是渲染出一堆方框让用户猜。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from .schedule_calendar import DayPlan, local_now
except ImportError:  # 插件以平铺模块方式加载时
    from schedule_calendar import DayPlan, local_now

# --------------------------------------------------------------------- 版式常量

WIDTH = 920
PAD = 32
CARD_GAP = 14
CARD_RADIUS = 10

DAY_HEADER_H = 44
SLOT_H = 58
EMPTY_ROW_H = 42
CARD_PAD = 16

TIME_COL_W = 132

BG = "#FFFFFF"
CARD = "#F7FAFD"
CARD_TODAY = "#EAF3FE"
BORDER = "#D9E5F2"
BORDER_TODAY = "#9CC3F0"
TEXT = "#1E293B"
MUTED = "#64748B"
ACCENT = "#2563EB"
BADGE_BG = "#2563EB"

# 系统 CJK 字体候选。顺序即优先级：先中文黑体，再思源/文泉驿，最后 macOS 回退。
_FONT_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

_FONT_SEARCH_ROOTS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".fonts"),
)


class ScheduleImageError(RuntimeError):
    """渲染失败（通常是缺少中文字体）。"""


@dataclass(slots=True)
class AgendaProfile:
    """图片头部展示的抬头信息。"""

    name: str = ""
    student_id: str = ""
    class_name: str = ""
    semester_label: str = ""
    days: int = 7
    semester_start: str = ""
    week_now: int | None = None


_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_font_path_cache: str | None = None


def _find_font_path() -> str:
    global _font_path_cache
    if _font_path_cache:
        return _font_path_cache

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            _font_path_cache = candidate
            return candidate

    for root in _FONT_SEARCH_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        for pattern in ("**/NotoSansCJK*", "**/*wqy*", "**/DroidSansFallback*"):
            for hit in sorted(base.glob(pattern)):
                if hit.suffix.lower() in {".ttc", ".ttf", ".otf"}:
                    _font_path_cache = str(hit)
                    return _font_path_cache

    raise ScheduleImageError(
        "未找到可用的中文字体，无法渲染课表图片。\n"
        "Debian/Ubuntu 请执行：apt-get install -y fonts-noto-cjk\n"
        "Alpine 请执行：apk add font-noto-cjk\n"
        "或把任意中文字体（如 msyh.ttc）放到 /usr/share/fonts/ 后重试。"
    )


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = _find_font_path()
    key = (path, size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    try:
        loaded = ImageFont.truetype(path, size)
    except OSError as exc:
        raise ScheduleImageError(f"加载字体失败：{path}（{exc}）") from exc
    _font_cache[key] = loaded
    return loaded


def fonts_available() -> bool:
    """探测系统是否有可用中文字体，供指令层提前给出友好提示。"""
    try:
        _find_font_path()
    except ScheduleImageError:
        return False
    return True


# --------------------------------------------------------------------- 文本工具


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    return int(draw.textlength(text, font=font))


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    if max_width <= 0 or _measure(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    budget = max_width - _measure(draw, ellipsis, font)
    if budget <= 0:
        return ellipsis
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _measure(draw, text[:mid], font) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low] + ellipsis


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_lines: int = 2,
) -> list[str]:
    """按像素宽度折行；中文没有词边界，逐字符累积即可。"""
    if not text:
        return []
    lines: list[str] = []
    current = ""
    for char in text:
        if _measure(draw, current + char, font) <= max_width:
            current += char
            continue
        lines.append(current)
        current = char
        if len(lines) == max_lines - 1:
            break
    remainder = text[len("".join(lines)) :] if lines else text
    if lines:
        lines.append(_ellipsize(draw, remainder, font, max_width))
    else:
        lines.append(_ellipsize(draw, current, font, max_width))
    return lines[:max_lines]


# --------------------------------------------------------------------- 渲染


def _card_height(plan: DayPlan) -> int:
    body = len(plan.slots) * SLOT_H if plan.slots else EMPTY_ROW_H
    return DAY_HEADER_H + body + CARD_PAD


def render_agenda(plans: list[DayPlan], profile: AgendaProfile) -> Image.Image:
    """把每日安排渲染成一张图片。"""
    title_font = _font(30)
    sub_font = _font(16)
    day_font = _font(20)
    badge_font = _font(15)
    name_font = _font(21)
    meta_font = _font(15)
    time_font = _font(17)

    inner_w = WIDTH - 2 * PAD

    # 先用一张临时画布测量文字，算出总高度，避免二次绘制
    probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    header_h = 96
    total_h = header_h + sum(_card_height(p) + CARD_GAP for p in plans) + 56

    image = Image.new("RGB", (WIDTH, total_h), BG)
    draw = ImageDraw.Draw(image)

    # 顶部标题
    draw.text((PAD, 34), "个人课表", font=title_font, fill=TEXT)
    head_parts = [
        p for p in (profile.name, profile.class_name, profile.semester_label) if p
    ]
    subtitle = " · ".join(head_parts)
    scope = f"最近 {profile.days} 天（含今天）"
    if profile.week_now:
        scope += f" · 当前第 {profile.week_now} 周"
    draw.text(
        (PAD, 74),
        f"{subtitle}    {scope}" if subtitle else scope,
        font=sub_font,
        fill=MUTED,
    )

    y = header_h
    for plan in plans:
        height = _card_height(plan)
        today = plan.is_today

        draw.rounded_rectangle(
            (PAD, y, PAD + inner_w, y + height),
            radius=CARD_RADIUS,
            fill=CARD_TODAY if today else CARD,
            outline=BORDER_TODAY if today else BORDER,
            width=1,
        )

        # 日期行
        left = f"{plan.date_label} {plan.weekday_name}"
        draw.text(
            (PAD + CARD_PAD, y + 12),
            left,
            font=day_font,
            fill=ACCENT if today else TEXT,
        )

        right_marks: list[str] = []
        if plan.week_label:
            right_marks.append(plan.week_label)
        right_text = " · ".join(right_marks)
        cursor = PAD + inner_w - CARD_PAD
        if today:
            badge = "今天"
            badge_w = _measure(probe, badge, badge_font) + 20
            draw.rounded_rectangle(
                (cursor - badge_w, y + 10, cursor, y + 10 + 26),
                radius=13,
                fill=BADGE_BG,
            )
            draw.text(
                (cursor - badge_w + 10, y + 16),
                badge,
                font=badge_font,
                fill="#FFFFFF",
            )
            cursor -= badge_w + 10
        if right_text:
            draw.text(
                (cursor - _measure(probe, right_text, meta_font), y + 16),
                right_text,
                font=meta_font,
                fill=MUTED,
            )

        body_y = y + DAY_HEADER_H
        if not plan.slots:
            draw.text((PAD + CARD_PAD, body_y + 10), "无课", font=meta_font, fill=MUTED)
            y += height + CARD_GAP
            continue

        for slot in plan.slots:
            course = slot.course
            draw.text(
                (PAD + CARD_PAD, body_y + 6),
                slot.time_label,
                font=time_font,
                fill=ACCENT,
            )
            draw.text(
                (PAD + CARD_PAD, body_y + 30),
                slot.period_label,
                font=meta_font,
                fill=MUTED,
            )

            text_x = PAD + CARD_PAD + TIME_COL_W
            text_w = PAD + inner_w - CARD_PAD - text_x
            name_lines = _wrap(draw, course.name, name_font, text_w, max_lines=1)
            draw.text((text_x, body_y + 6), name_lines[0], font=name_font, fill=TEXT)

            details = [p for p in (course.place, course.teacher) if p]
            if course.weeks:
                details.append(course.weeks)
            meta = " · ".join(details)
            if meta:
                draw.text(
                    (text_x, body_y + 34),
                    _ellipsize(draw, meta, meta_font, text_w),
                    font=meta_font,
                    fill=MUTED,
                )
            body_y += SLOT_H

        y += height + CARD_GAP

    footer = f"生成于 {local_now().strftime('%Y-%m-%d %H:%M')}"
    if not profile.semester_start:
        footer += "　·　未设置开学日期，未按周次过滤"
    draw.text((PAD, y + 8), footer, font=meta_font, fill=MUTED)

    return image


def save_agenda(
    plans: list[DayPlan],
    profile: AgendaProfile,
    directory: Path,
    *,
    keep: int = 20,
) -> Path:
    """渲染并落盘，同时清理旧的渲染结果，避免目录无限膨胀。"""
    image = render_agenda(plans, profile)
    render_dir = directory / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    stamp = local_now().strftime("%Y%m%d_%H%M%S")
    path = render_dir / f"agenda_{stamp}.png"
    image.save(path, format="PNG", optimize=True)

    old = sorted(
        render_dir.glob("agenda_*.png"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for stale in old[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return path
