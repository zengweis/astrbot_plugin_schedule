# astrbot_plugin_schedule

浙大宁波理工学院教务系统个人课表插件（AstrBot）：导入课表、渲染课表图片、上课前提醒。

## 指令

| 指令 | 说明 |
| --- | --- |
| `设置信息 <学号/账号> <密码>` | 保存教务系统登录凭据。不带参数时显示当前保存状态。 |
| `导入个人课表` | 登录教务系统，抓取**最新学期**课表与节次作息，格式化后写入本地文件。 |
| `我的课表 [天数]` | 把最近 N 天（**含今天**）的课程渲染成图片，默认 7 天，上限 60 天。 |
| `设置开学 <YYYYMMDD>` | 指定本学期第一周周一的日期，供按周次过滤使用。 |
| `wakeup <提前分钟>` | 设定每节课提前多少分钟提醒；`wakeup 0` 关闭。不带参数时显示当前设定。 |

推荐的使用顺序：

```
设置信息 accountid yourpassword
导入个人课表
设置开学 20260914<date>
我的课表 7
wakeup 15
```

## 安装

在 AstrBot 插件目录下克隆本仓库：

```bash
cd AstrBot/data/plugins
git clone https://github.com/zengweis/astrbot_plugin_schedule.git
```

依赖由 AstrBot 依据 `requirements.txt` 自动安装：

```
httpx>=0.27
pycryptodome>=3.20
pillow>=10.0
```

### 「我的课表」需要中文字体

渲染图片依赖系统里的 CJK 字体。Linux 服务器上通常需要手动装一次，否则中文会渲染失败
（插件会明确报错并给出安装命令，而不是输出一堆方框）：

```bash
# Debian / Ubuntu
apt-get install -y fonts-noto-cjk
# Alpine
apk add font-noto-cjk
```

插件会按以下顺序自动查找：Windows 的 `msyh.ttc` / `simhei.ttf`、Linux 的
`NotoSansCJK*` / 文泉驿 / DroidSansFallback、macOS 的 `PingFang.ttc`。

## 数据落盘

全部写在 AstrBot 的插件数据目录，**不写插件自身目录**（避免更新/重装时被覆盖）：

```
AstrBot/data/plugin_data/astrbot_plugin_schedule/
├── credentials.json            # 账号凭据
├── settings.json               # wakeup 提前量、开学日期、提醒推送的会话
├── periods.json                # 节次作息表（第几节 = 几点几分）
├── timetable.json              # 最新一次导入的课表（机器可读）
├── timetable.md                # 最新一次导入的课表（人类可读，含周视图）
├── timetable_2026-2027-1.json  # 按学期归档的快照
└── render/agenda_*.png         # 「我的课表」渲染结果，自动只保留最近 20 张
```

`timetable.json` 中的 `courses[]` 每项形如：

```jsonc
{
  "name": "审美与造型综合基础",
  "teacher": "白",
  "weekday": 1,
  "weekday_name": "星期一",
  "sections": "1-4",
  "start_section": 1,
  "end_section": 4,
  "weeks": "1-3周,5-9周",
  "room": "SA202-1",
  "credits": "3.0",
  "code": "20253025"
}
```

## 工作原理

教务系统仅内网可达（`jwxt.nbt.edu.cn` 无公网解析），必须经过 WebVPN，共三层门：

```
WebVPN 门户            webvpn.nbt.edu.cn
      ↓
统一身份认证（CAS）     authserver-443.webvpn.nbt.edu.cn   ← 账号 + AES 加密口令
      ↓
正方教务 jwglxt        jwxt-443.webvpn.nbt.edu.cn          ← 学生课表查询
```

登录细节由抓取登录页前端逻辑得到，非猜测：

- 口令不是明文提交。登录页给一个隐藏域 `pwdEncryptSalt`，前端把
  `随机64位前缀 + 明文密码` 用 AES-128-CBC（key = salt，iv = 随机16位，PKCS7）加密后 Base64，
  再提交到 `password` 字段。实现见 `schedule_crypto.py`。
- 登录成功后还要依次访问「主框架页 → 学生课表页」，否则课表数据接口会返回 901。
- 课表接口为 `POST /jwglxt/kbcx/xskbcx_cxXsgrkb.html`，参数
  `xnm`（学年）、`xqm`（学期，3/12/16 分别代表 1/2/3 学期）、`kzlx=ck`。
- 节次作息来自 `POST /jwglxt/kbcx/xskbcx_cxRjc.html`，给出每一节的起止时刻，
  是把「第 3-4 节」换算成「09:50-11:25」的依据。

「最新学期」取课表页学年/学期下拉框中被默认选中的那一组。

### 为什么需要手工指定开学日期

正方教务**对学生开放的接口不包含校历**，实测过：

- 课表响应里的 `rqazcList`、`djdzList` 恒为空数组；
- `/jwglxt/xtgl/index_cxXlList.html`、`xskbcx_cxXnxqzc.html` 一类路径要么返回「警告提示」
  页面，要么返回 910 业务错误；
- 给课表接口追加 `zc` / `zcd` 参数，响应里的日期字段依然是当天，不会随周次变化；
- 课表页 HTML 与 `xskbcx.js` 里都没有周次到日期的换算逻辑。

没有「第 1 周周一」就无法判断某个周次对应哪几天，因此这一步只能由用户确认：

```
设置开学 20260914
```

输入会被归一到所在周的周一（填报到日也不会错）。插件内置了一张已知学期的校历表作为提示，
键为 `(xnm, xqm)`，见 `schedule_calendar.KNOWN_SEMESTER_STARTS`，用户设置优先于它。

## 上课提醒

`wakeup 15` 之后，插件在后台每 30 秒检查一次，在每节课开始前 15 分钟把提醒推到
**发起 `wakeup` 的那个会话**：

```
15 分钟后上课
审美与造型综合基础
08:00-11:25 · 第1-4节
地点：SA202-1
教师：白
```

同一节课只会提醒一次（按 `日期|课程|节次|开始时刻` 幂等去重，跨天自动重置）。
提醒任务在 `initialize()` 中随插件启动，`terminate()` 时取消。

## 健壮性

WebVPN 链路上网络抖动与页面半加载是常态，插件对以下情况都做了处理：

- 连接重置、读超时：自动重试 3 次，最终统一收敛为可读错误提示。
- 登录成功但课表页未返回真实内容：就地重试 3 次；仍失败则清空会话整体重登一次。
- 数据接口返回 901 或空响应：自动重新登录并重试一次。
- 账号密码错误、需要滑块验证码、数据目录不可写、缺少中文字体：分别给出明确的用户可读提示。
- 提醒循环内部吞掉所有异常并记日志——后台任务崩溃不会影响机器人本体。
- 所有指令异常都被兜住，不会因单个异常导致机器人崩溃。

## 已知限制

- **滑块验证码**：统一身份认证在触发风控时会要求滑块校验，无法无人值守通过。此时插件会
  明确提示，需先用浏览器手动登录一次后再试。
- **调课/停课不反映**：图片按「每周固定 + 周次」展开，学校临时调整上课日期（例如校历里
  「9月20日上第二周周五的课」这类安排）不会体现，以教务系统实际通知为准。
- **仅支持浙大宁波理工学院**。主机名与接口路径集中在 `schedule_client.py` 的类属性里，
  结构上分离得比较干净，改造成其他学校的正方教务系统需要替换这些常量并重新验证登录流程。
- 提醒依赖机器人进程常驻；进程停止期间不会补发错过的提醒。

## 安全说明

统一身份认证没有长期令牌，每次抓取都要用口令重新登录，因此**密码以可逆形式保存在
`credentials.json`**（POSIX 下权限 0600）。请知悉：

- 同机上能读取该文件的其他进程可以获得你的教务账号。
- 在群聊中发送「设置信息」会把口令暴露在聊天记录里，插件检测到非私聊会话时会拒绝保存。
- 不再使用时请直接删除 `credentials.json`。

## 合规提示

本插件以自动化方式登录学校教务系统。请自行确认这种做法符合你所在学校的账号使用与
信息系统管理规定；插件只做读操作，不会选课、改课表或修改账号数据。

## 开发

插件遵循 AstrBot 官方插件开发规范，提交前用 ruff 格式化与检查：

```bash
ruff format . && ruff check .
```

作者：JERRY WEI
