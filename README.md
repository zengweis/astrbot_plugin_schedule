# astrbot_plugin_schedule

浙大宁波理工学院教务系统个人课表导入插件（AstrBot）。

发送「设置信息」保存教务系统账号密码，发送「导入个人课表」自动登录、抓取最新学期课表，
做数据处理与格式化后保存到本地文件。

## 指令

| 指令 | 说明 |
| --- | --- |
| `设置信息 <学号/账号> <密码>` | 保存教务系统登录凭据。不带参数时显示当前保存状态。 |
| `导入个人课表` | 登录教务系统，抓取**最新学期**课表，格式化后写入本地文件，并在聊天窗口回一份摘要。 |

使用示例：

```
设置信息 3260227024 yourpassword
导入个人课表
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
```

安装后到 WebUI 插件管理页重载插件即可。

## 数据落盘

所有数据写在 AstrBot 的插件数据目录，**不写插件自身目录**（避免更新/重装时被覆盖）：

```
AstrBot/data/plugin_data/astrbot_plugin_schedule/
├── credentials.json          # 账号凭据
├── timetable.json            # 最新一次导入的课表（机器可读）
├── timetable.md              # 最新一次导入的课表（人类可读，含周视图）
└── timetable_2026-2027-1.json  # 按学期归档的快照
```

`timetable.json` 结构：

```jsonc
{
  "student_name": "曾蔚",
  "student_id": "3260227024",
  "class_name": "数字媒体艺术261",
  "semester_label": "2026-2027 学年 第1学期",
  "fetched_at": "2026-09-13 23:24:15",
  "courses": [
    {
      "name": "审美与造型综合基础",
      "teacher": "白晓霞",
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
  ]
}
```

`timetable.md` 里除同样字段外，还多一张跨节次合并显示的周视图表格。

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

- 密码不是明文提交。登录页给一个隐藏域 `pwdEncryptSalt`，前端把
  `随机64位前缀 + 明文密码` 用 AES-128-CBC（key = salt，iv = 随机16位，PKCS7）加密后 Base64，
  再提交到 `password` 字段。实现见 `schedule_crypto.py`。
- 登录成功后还要依次访问「主框架页 → 学生课表页」，否则课表数据接口会返回 901。
- 课表接口为 `POST /jwglxt/kbcx/xskbcx_cxXsgrkb.html`，参数
  `xnm`（学年）、`xqm`（学期，3/12/16 分别代表 1/2/3 学期）、`kzlx=ck`。

「最新学期」取课表页学年/学期下拉框中被默认选中的那一组。

## 健壮性

WebVPN 链路上网络抖动与页面半加载是常态，插件对以下情况都做了处理：

- 连接重置、读超时：自动重试 3 次，最终统一收敛为可读错误提示。
- 登录成功但课表页未返回真实内容：就地重试 3 次；仍失败则清空会话整体重登一次。
- 数据接口返回 901 或空响应：自动重新登录并重试一次。
- 账号密码错误、需要滑块验证码、数据目录不可写等：分别给出明确的用户可读提示。
- 所有指令异常都被兜住并记日志，不会因单个异常导致机器人崩溃。

## 已知限制

- **滑块验证码**：统一身份认证在触发风控时会要求滑块校验，无法无人值守通过。此时插件会
  明确提示，需先用浏览器手动登录一次后再试。
- **仅支持浙大宁波理工学院**。主机名与接口路径写死在 `schedule_client.py` 的类属性里，
  结构上分离得比较干净，改造成其他学校的正方教务系统需要替换这些常量并重新验证登录流程。
- 未实现学期查询、单日课表、课程提醒等功能，当前只覆盖「导入个人课表」。

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
