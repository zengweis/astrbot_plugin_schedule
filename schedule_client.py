"""浙大宁波理工学院教务系统客户端。

访问路径（三层门，逐层校验）：

    1. WebVPN 门户      https://webvpn.nbt.edu.cn              —— 与 2、3 同为同一台设备
    2. 金智统一身份认证  https://authserver-443.webvpn.nbt.edu.cn —— 账号 + AES 加密口令
    3. 正方教务 jwglxt   https://jwxt-443.webvpn.nbt.edu.cn      —— 内网，无公网解析

登录成功后依次访问「主框架页 → 学生课表页」，最后请求课表数据接口。
课表页必须先访问，否则数据接口会返回 901——教务系统靠页面上下文判定会话归属。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote

import httpx

try:
    from .schedule_calendar import PeriodTime, parse_periods
    from .schedule_crypto import encrypt_password
    from .schedule_parser import Timetable, parse_timetable
except ImportError:  # 插件以平铺模块方式加载时
    from schedule_calendar import PeriodTime, parse_periods
    from schedule_crypto import encrypt_password
    from schedule_parser import Timetable, parse_timetable


class JwglxtError(RuntimeError):
    """教务系统交互失败的基类。"""


class JwglxtAuthError(JwglxtError):
    """账号或密码不被接受。"""


class JwglxtCaptchaRequired(JwglxtError):
    """统一身份认证要求人机校验（滑块验证码），无法无人值守通过。"""


class JwglxtSessionExpired(JwglxtError):
    """已登录但教务系统侧会话失效，且自动重登后仍未恢复。"""


class JwglxtPageNotReady(JwglxtError):
    """内部信号：课表页没有返回真实内容，需要重开会话。"""


def _attr(html: str, elem_id: str) -> str:
    """取出指定 id 的 input 元素的 value，容忍属性顺序差异。"""
    tag = re.search(rf'<input[^>]*id="{re.escape(elem_id)}"[^>]*>', html)
    if not tag:
        return ""
    value = re.search(r'value="([^"]*)"', tag.group(0))
    return value.group(1) if value else ""


def _var(html: str, name: str) -> str:
    m = re.search(rf'var\s+{re.escape(name)}\s*=\s*"([^"]*)"', html)
    return m.group(1) if m else ""


def _is_login_page(html: str) -> bool:
    """登录页特征：带加密盐值的表单仍在，说明登录没成功。

    除标准登录表单外，再兜一层——WebVPN 侧的 CAS 中转页也带「统一身份认证」字样，
    漏判会让后续解析拿到空页面。
    """
    if "pwdEncryptSalt" in html and "loginFromId" in html:
        return True
    return "统一身份认证" in html and "/authserver/login" in html


def _extract_error(html: str) -> str:
    for pattern in (
        r'id="showErrorTip"[^>]*>\s*([^<]{2,80}?)\s*<',
        r'id="msg"[^>]*>\s*([^<]{2,80}?)\s*<',
        r'id="loginErrorTip"[^>]*>\s*([^<]{2,80}?)\s*<',
    ):
        m = re.search(pattern, html)
        if m and m.group(1).strip():
            return m.group(1).strip()
    for hint in (
        "用户名或密码错误",
        "密码错误",
        "账号或密码错误",
        "用户不存在",
        "账号被锁定",
        "尚未激活",
    ):
        if hint in html:
            return hint
    return "账号或密码错误，或该账号尚未激活"


def _selected_option(html: str, select_id: str) -> str:
    m = re.search(
        rf'<select[^>]*id="{re.escape(select_id)}".*?</select>',
        html,
        re.DOTALL,
    )
    if not m:
        return ""
    chosen = re.search(r'<option[^>]*value="([^"]*)"[^>]*selected', m.group(0))
    if chosen:
        return chosen.group(1)
    first = re.search(r'<option[^>]*value="([^"]*)"', m.group(0))
    return first.group(1) if first else ""


def _options(html: str, select_id: str) -> list[tuple[str, str]]:
    m = re.search(
        rf'<select[^>]*id="{re.escape(select_id)}".*?</select>', html, re.DOTALL
    )
    if not m:
        return []
    return [
        (v, t)
        for v, t in re.findall(
            r'<option[^>]*value="([^"]*)"[^>]*>(.*?)</option>', m.group(0), re.DOTALL
        )
    ]


class JwglxtClient:
    """课表抓取客户端。用法：`async with JwglxtClient(user, pwd) as c: tt = await c.import_timetable()`"""

    AUTH_BASE = "https://authserver-443.webvpn.nbt.edu.cn"
    JW_BASE = "https://jwxt-443.webvpn.nbt.edu.cn"
    WEBVPN_BASE = "https://webvpn.nbt.edu.cn"
    CAS_SERVICE = "https://webvpn.nbt.edu.cn/users/auth/cas/callback?url"

    MENU_PATH = "/jwglxt/xtgl/index_initMenu.html"
    KB_PAGE_PATH = "/jwglxt/kbcx/xskbcx_cxXskbcxIndex.html?gnmkdm=N2151"
    KB_QUERY_PATH = "/jwglxt/kbcx/xskbcx_cxXsgrkb.html"
    PERIODS_PATH = "/jwglxt/kbcx/xskbcx_cxRjc.html"
    # 课表页必须含学年下拉框，作为「页面是否真的加载成功」的判据
    KB_PAGE_MARKER = 'id="xnm"'

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, username: str, password: str, *, timeout: float = 45.0) -> None:
        self.username = (username or "").strip()
        self.password = password or ""
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._kb_page_html = ""
        self._logged_in = False
        # 最近一次查询的上下文，供节次作息表等附属接口复用
        self.current_year = ""
        self.current_term = ""
        self.campus_id = ""

    # ---------------------------------------------------------------- 生命周期

    async def __aenter__(self) -> "JwglxtClient":  # noqa: PYI034, UP037 — 需兼容 Python 3.10，不能用 typing.Self
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
            headers={"User-Agent": self.USER_AGENT},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._logged_in = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise JwglxtError(
                "客户端尚未初始化，请使用 `async with JwglxtClient(...)` 或先调用 login()"
            )
        return self._client

    # ---------------------------------------------------------------- 请求封装

    async def _request(
        self, method: str, url: str, *, attempts: int = 3, **kwargs: Any
    ):
        """带重试的 HTTP 请求。

        WebVPN 链路上连接被重置、读超时都是常态，不重试会让插件动不动就报错；
        重试仍失败则统一收敛成 `JwglxtError`，避免把 httpx 的原始堆栈抛给用户。
        """
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(1.0 * attempt)
        raise JwglxtError(
            f"网络请求失败（已重试 {attempts} 次）：{last_error}"
        ) from last_error

    async def _get(self, url: str, **kwargs: Any):
        return await self._request("GET", url, **kwargs)

    async def _post(self, url: str, **kwargs: Any):
        return await self._request("POST", url, **kwargs)

    # ---------------------------------------------------------------- 登录

    @property
    def _cas_login_url(self) -> str:
        return (
            f"{self.AUTH_BASE}/authserver/login"
            f"?type=userNameLogin&service={quote(self.CAS_SERVICE, safe='')}"
        )

    @property
    def _cas_post_url(self) -> str:
        return f"{self.AUTH_BASE}/authserver/login?service={quote(self.CAS_SERVICE, safe='')}"

    async def login(self, *, _retry: bool = True) -> None:
        """完成统一身份认证并进入教务系统，建立后续请求所需的会话上下文。

        WebVPN 下偶发「登录成功但课表页没拿到内容」，此时清空会话重走一遍整个流程。
        """
        try:
            await self._do_login()
        except JwglxtPageNotReady as exc:
            if not _retry:
                raise JwglxtError(f"登录后未能进入课表页：{exc}") from exc
            self.client.cookies.clear()
            self._logged_in = False
            self._kb_page_html = ""
            await self.login(_retry=False)

    async def _do_login(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                headers={"User-Agent": self.USER_AGENT},
            )

        page = await self._get(self._cas_login_url)
        html = page.text

        if not _is_login_page(html):
            # 已有有效 CAS 会话（例如复用了 cookie），跳过表单提交
            await self._enter_jwglxt()
            return

        need_captcha = _var(html, "needCaptcha").strip()
        if need_captcha:
            raise JwglxtCaptchaRequired(
                "统一身份认证当前要求人机校验（滑块验证码），无法自动登录。"
                "请先用浏览器手动登录一次，或稍后再试。"
            )

        salt = _attr(html, "pwdEncryptSalt")
        if not salt:
            raise JwglxtError(
                "未能从统一身份认证页面解析出加密盐值 pwdEncryptSalt，页面结构可能已变更。"
            )

        try:
            encrypted = encrypt_password(self.password, salt)
        except ValueError as exc:
            raise JwglxtError(f"口令加密失败：{exc}") from exc

        response = await self._post(
            self._cas_post_url,
            data={
                "username": self.username,
                "password": encrypted,
                "captcha": "",
                "rememberMe": "true",
                "weblogin": "true",
                "_eventId": "submit",
                "cllt": "userNameLogin",
                "dllt": "generalLogin",
                "lt": "",
                "execution": _attr(html, "execution"),
            },
            headers={"Referer": self._cas_login_url, "Origin": self.AUTH_BASE},
        )

        if _is_login_page(response.text):
            raise JwglxtAuthError(_extract_error(response.text))

        await self._enter_jwglxt()

    async def _enter_jwglxt(self) -> None:
        """访问主框架页与课表页，让教务系统把会话与「课表查询」功能绑定起来。

        课表页必须先于数据接口访问，否则数据接口返回 901。实测 WebVPN 下这一步有约 1/3 的
        概率拿到不含表单的中间页，因此这里做「校验 + 就地重试」，重试仍失败则上抛
        `JwglxtPageNotReady`，由 login() 重开会话。
        """
        menu = await self._get(
            self.JW_BASE + self.MENU_PATH,
            headers={"Referer": self.WEBVPN_BASE + "/"},
        )
        if _is_login_page(menu.text):
            raise JwglxtAuthError("进入教务系统失败，登录态未被接受，请检查账号密码。")

        self._kb_page_html = await self._load_kb_page()
        self._logged_in = True

    async def _load_kb_page(self, *, attempts: int = 3) -> str:
        """拉取课表页，直到拿到含学年下拉框的真实内容。"""
        last_excerpt = ""
        for attempt in range(1, attempts + 1):
            response = await self._get(
                self.JW_BASE + self.KB_PAGE_PATH,
                headers={"Referer": self.JW_BASE + self.MENU_PATH},
            )
            html = response.text or ""
            if self.KB_PAGE_MARKER in html:
                return html
            if _is_login_page(html):
                raise JwglxtAuthError("教务系统会话无效，请重新设置账号信息。")
            last_excerpt = " ".join(html[:120].split())
            if attempt < attempts:
                await asyncio.sleep(1.5 * attempt)

        raise JwglxtPageNotReady(
            f"连续 {attempts} 次未取到课表页内容（HTTP {response.status_code}）："
            f"{last_excerpt or '响应为空'}"
        )

    # ---------------------------------------------------------------- 学期

    def available_semesters(self) -> list[dict[str, str]]:
        """从课表页解析可选学年学期。"""
        years = _options(self._kb_page_html, "xnm")
        terms = _options(self._kb_page_html, "xqm")
        return [
            {"year": yv, "year_name": yt, "term": tv, "term_name": tt}
            for yv, yt in years
            if yv
            for tv, tt in terms
            if tv
        ]

    def default_semester(self) -> tuple[str, str]:
        """取课表页默认选中的学年与学期，即「最新」的那一组。"""
        year = _selected_option(self._kb_page_html, "xnm")
        term = _selected_option(self._kb_page_html, "xqm")
        if not year:
            raise JwglxtError("未能解析课表页的学年下拉框，无法确定默认学期。")
        return year, term or "3"

    # ---------------------------------------------------------------- 课表

    async def fetch_raw(
        self, year: str, term: str, *, retry: bool = True
    ) -> dict[str, Any]:
        """请求课表数据接口，返回原始 JSON。

        接口偶发返回 901（会话上下文丢失），此时自动重新登录并重试一次。
        """
        if not self._logged_in:
            await self.login()

        kb_page_url = self.JW_BASE + self.KB_PAGE_PATH
        response = await self._post(
            self.JW_BASE + self.KB_QUERY_PATH,
            data={
                "xnm": year,
                "xqm": term,
                "kzlx": "ck",
                "xsdm": "",
                "kclbdm": "",
                "kclxdm": "",
            },
            headers={
                "Referer": kb_page_url,
                "Origin": self.JW_BASE,
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        body = (response.text or "").strip()
        broken = (
            response.status_code == 901
            or not body
            or body == "null"
            or _is_login_page(body)
        )

        if broken:
            if retry:
                self._logged_in = False
                await self.login()
                return await self.fetch_raw(year, term, retry=False)
            raise JwglxtSessionExpired(
                f"课表接口未返回数据（HTTP {response.status_code}）。"
                "教务系统会话可能已失效，请稍后重试。"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise JwglxtError(
                "课表接口返回的不是合法 JSON，可能被 WebVPN 拦截或页面改版。"
            ) from exc

        # 记下本次上下文，供 fetch_periods 复用（节次作息表按校区区分）
        self.current_year, self.current_term = year, term
        for item in payload.get("kbList") or []:
            campus = str(item.get("xqh_id") or "").strip()
            if campus:
                self.campus_id = campus
                break
        return payload

    async def import_timetable(self, year: str = "", term: str = "") -> Timetable:
        """抓取并解析课表；未指定学年学期时取页面默认值（最新）。"""
        if not self._logged_in:
            await self.login()
        if not year:
            year, term = self.default_semester()
        payload = await self.fetch_raw(year, term)
        timetable = parse_timetable(payload)

        # 页面下拉框比接口回显更可靠：空课表时接口可能不带 xsxx
        timetable.year = timetable.year or year
        timetable.term = timetable.term or term
        for option in self.available_semesters():
            if option["year"] == timetable.year and not timetable.year_name:
                timetable.year_name = option["year_name"]
            if option["year"] == timetable.year and option["term"] == timetable.term:
                timetable.term_name = timetable.term_name or option["term_name"]
        return timetable

    async def fetch_periods(self, year: str = "", term: str = "") -> list[PeriodTime]:
        """抓取节次作息表，用来把「第 3-4 节」换算成具体时刻。

        参数缺失时沿用最近一次课表查询的学年/学期/校区。
        """
        if not self._logged_in:
            await self.login()
        response = await self._post(
            self.JW_BASE + self.PERIODS_PATH,
            data={
                "xnm": year or self.current_year,
                "xqm": term or self.current_term,
                "xqh_id": self.campus_id,
            },
            headers={
                "Referer": self.JW_BASE + self.KB_PAGE_PATH,
                "Origin": self.JW_BASE,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        try:
            payload = response.json()
        except ValueError:
            return []
        return parse_periods(payload)
