# 个人文档问答助手（docqa）

在对话框里选/拖入 1 个或多个文档，然后针对文档提问，由 **DeepSeek Harness agent** 阅读文档后作答。
本仓库是一个可直接运行的最小实现：**FastAPI + SSE 的 Web 界面**（`docqa/static/index.html` 单文件前端），
外加命令行与 Streamlit 两个备用入口，核心逻辑在 `docqa/engine.py`。

- **文档不进消息通道，而是进工作目录**：上传 → 逐字节保留原文 → 转成 Markdown → agent 用自己的 `read`/`grep` 工具阅读。
- **不做向量库**：几个到几十个文件、几百 KB 到几 MB 的语料，内联或 grep 就够（见「架构与设计」）。
- **真 token 级流式**：runtime 内的 token 增量经转发插件回到宿主，再以 SSE `delta` 事件推进浏览器（见「token 级流式」）。
- **GenUI 结构化回答**：模型用 ` ```dsh-ui ` 围栏输出 JSON，前端渲染成表格 / 图表 / 步骤 / quiz / mermaid / 3D 等组件。
- **三层格式转换链**：原生精确解析 → LibreOffice 无头 → MarkItDown，逐层兜底，按魔数嗅探而非扩展名。
- **每轮回答标来源**：文件名 + 页号/章节/工作表名；文档里没有的就回答「文档中未提及」。

> **⚠ Windows + pip 装的打包运行时（默认路线）先读这条：** 启动必须带
> `--permission-mode danger-full-access`（或在 `.env` 里写 `DOCQA_PERMISSION_MODE=danger-full-access`），
> 否则 agent 的 `pwsh` / `grep` / `glob` 会在沙箱 runner 组装阶段失败（`error: --profile <name> is required`）。
> 原因与三条出路见 **[Windows 沙箱 runner](#windows-沙箱-runnershell-与检索为什么需要-danger-full-access)**。

---

## 一、项目简介

### 它是什么

DSH（DeepSeek Harness）的 Python SDK 是**无头 agent 驱动器**，不是聊天应用：它没有界面，也没有文件上传入口。
（`dsh web` 那个 Web GUI 是独立应用，不能为 Python SDK 客户端服务。）所以这件事必须拆成两半：

| 由谁负责 | 做什么 |
|---|---|
| 本仓库（宿主侧） | 对话框、附件上传、文件落盘、格式转换、会话管理、事件流与界面渲染 |
| DSH SDK / agent | 读文件、检索、推理、作答、持久化会话与上下文压缩 |

关键约束：**上传的文档不能走 SDK 的消息通道**（协议只允许文本块与栅格图片块），
必须以「工作目录里的文件」形式存在，让 agent 用自己的 `read` / `grep` 去读。这是整个方案成立的前提。
**图片是唯一的例外**：它没有被转成 Markdown，作为图像块随问题一起内联送出（见「图片走视觉通道」）。

### 数据流

```
① 选文件 / 拖文件        ──▶  附件暂存（挂在输入框上方，尚未入库）
② 输入问题并发送          ──▶  POST /api/documents（附件在这一刻才入库）
                              ├─ uploads/<sha8>-<name>   原文，逐字节保留
                              └─ docs/<sha8>-<name>.md   三层转换结果 + INDEX.md 清单
                         ──▶  POST /api/chat {question, images:[图片 slug]} ──▶ 202 {job_id}
                              └─ 宿主装配 prompt = 作答纪律 + 清单 + （全文 | 只给清单）
                                                   + 图片说明 + 图像块 + 问题
                                   │
                                   │  stdio JSON-RPC
                                   ▼
                              dsh 运行时：agent 用 read / grep 阅读文档并推理
                                   │
                                   │  session.event / session.status
                                   │  ＋ 转发插件送回的 token 增量（delta）
                                   ▼
③ GET /api/chat/{job_id}/events（SSE，断线可补发）
                         ──▶  前端：事件行 + token 流式正文 + GenUI 组件
```

### 它不是什么

- 不是多用户 / 带权限隔离的 SaaS：没有账号体系，资料库是单个本地目录。
- 不是 RAG 系统：没有向量库、没有嵌入模型、没有召回率问题（也换不来海量语料的检索速度）。
- 不是「上传即入库」：选中的文件先挂在输入框，**按下发送才入库并进入对话**。

---

## 二、应用场景

### 适合

| 场景 | 为什么合适 | 典型语料 |
|---|---|---|
| 个人资料问答 | 语料小、问题碎、要求标来源 | 合同、保单、说明书、论文、笔记 |
| 小团队内部知识库 | 十几个文件、随手更新，不值得搭 RAG | 运维手册、SOP、接口文档、周报 |
| 财报 / 报表核对 | 表格结构被保留成 Markdown 表格，模型能跨表比较 | `.xlsx` / `.csv` / 带表格的 `.docx` / `.pdf` |
| 会议与评审材料速读 | 一次多文件上传，回答会分文件定位再对比 | `.pptx`（含演讲者备注）、`.docx`、`.pdf` |
| 需要**结构化呈现**的问答 | GenUI 围栏可直接产出表格 / 指标卡 / 步骤 / 图表 / 自测 quiz | 任何「对比、清单、流程、数据」型问题 |
| 离线 / 内网环境 | 前端零外链；SDK 运行时可走本地构建，模型可指向兼容代理 | — |

### 不适合

| 场景 | 原因 |
|---|---|
| 上千文件 / 单文件几十万字符 | grep 也扫不动了，该上向量检索（Milvus + 嵌入模型） |
| 要求毫秒级首字 | 每轮都是一次完整 agent 调用；首轮还要拉起 runtime 子进程 |
| 多用户并发 / 权限隔离 | 单进程串行执行（同时只允许一轮），资料库是共享目录 |
| 扫描件 / 纯图片资料 | 图片不做 OCR；但会作为图像块随问题内联送给视觉模型（需 `read_image`/图片输入可用的模型），文字识别仍建议上 tesseract |
| 需要 token 级"边想边说" | 思考与正文的**提交事件**按步到达（流式增量已实现，但步末会被权威整段替换） |

---

## 三、项目目录结构

### 仓库

```
docqa/
├─ docqa/                        # 包本体（源程序）
│  ├─ engine.py                  # 核心：DeepSeekHarness 封装、prompt 装配、会话与轮换、线程化流式
│  ├─ ingest.py                  # 宿主侧：原文落盘 + 三层转换链 + INDEX.md + 引擎自检
│  ├─ events.py                  # 事件契约：TurnEvent + SDK 通知 → 事件的翻译器
│  ├─ server.py                  # FastAPI：REST + SSE + 两个内部钩子端点
│  ├─ static/index.html          # 单文件前端（vanilla JS，零外链：Markdown / 事件行 / 流式 / GenUI 渲染器）
│  ├─ plugin/                    # 叠在 sdk profile 上的两层 cordis 覆写
│  │  ├─ cordis.patch.yml         # → 挂 docqa-stream-forward（token 增量转发）
│  │  ├─ docqa-stream-forward.mjs #   订阅 agent/assistant-stream，攒批 POST 回宿主
│  │  ├─ genui.patch.yml          # → 挂 dsh-genui 宿主半边
│  │  └─ genui/                   #   vendored：index.js（MIT）+ SKILL.md + LICENSE + UPSTREAM-package.json
│  ├─ app.py                     # Streamlit 界面（早期版本，保留可用）
│  ├─ cli.py                     # 命令行：ingest / ask / reset / status / engines
│  └─ __init__.py
├─ run_web.py                    # Web 服务入口（.env 加载、依赖、凭据、权限模式、两层 patch、资产路径都在这里装配）
├─ requirements.txt
├─ .env.example                  # 配置模板：复制成 .env（.env 含密钥，不要提交）
├─ README.zh.md
├─ .venv/                        # 运行时环境（Python 3.11 + 依赖，约 785 MB）
└─ .docqa-web/                   # 运行期资料库与运行时状态（见下）
```

### 运行期目录（默认 `./.docqa-web`，`--library` 可改）

```
.docqa-web/
├─ workspace/                    # 传给 SDK 的 cwd，也是 agent 的工作目录
│  ├─ uploads/<sha8>-<name>      # 原文，逐字节保留
│  ├─ docs/<sha8>-<name>.md      # 转换结果，agent 实际阅读的对象
│  └─ INDEX.md                   # 清单：每个文件命中的转换引擎、字符数、页数、备注
├─ home/                         # 隔离的 DSH_HOME：profile、会话日志、投影缓存
│  ├─ profiles/sdk/              # sdk profile（首轮生成，含 bundled 插件）
│  ├─ sessions/*/*.jsonl.zstd    # 会话事件日志（历史档案，不能作为续接通道）
│  └─ storages/session_projcache/# token 用量、上下文压力、上下文分解等投影
├─ .cache/
│  ├─ loprofile/                 # LibreOffice 用户配置（重定向进工作区，必须）
│  └─ pkg-native/                # runtime 原生模块解包缓存（约 19.5 MB，可随时删）
└─ state.json                    # 会话 id、runtime token、文档索引
```

### 关键文件职责

| 文件 | 职责 | 改动时要注意 |
|---|---|---|
| `docqa/engine.py` | `EngineConfig`、prompt 两条装配路径、session 轮换与自愈、`ask_stream()` | `SYSTEM_RULES` 里的作答纪律会直接影响答案质量 |
| `docqa/ingest.py` | 魔数嗅探、三层转换、幂等去重（`sha256[:8]`）、`engine_availability()` | 新增格式只需在对应层加分支 |
| `docqa/events.py` | `TurnEvent`、`summarize_arguments()`、`TurnTranslator.feed()` | 前端与 CLI 都依赖这里的字段名 |
| `docqa/server.py` | 作业注册表（job → 事件缓冲 + SSE 扇出）、REST、两个内部钩子 | 作业与事件缓冲都在进程内存里，**不能多 worker** |
| `docqa/static/index.html` | 全部前端：Markdown 渲染、事件行、token 流式、GenUI 渲染器、附件暂存 | 单文件、无构建步骤；`node --check` 可做语法自检 |
| `run_web.py` | 参数解析、凭据回退、权限模式装配、两层 `--patch`、genui 资产目录 | 新增插件覆写层在这里挂载 |

---

## 四、如何部署

### 4.1 环境要求

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.11**（3.10+ 应可） | 本项目在 3.11.9 上开发验证 |
| 操作系统 | Windows / macOS / Linux | 开发环境是 Windows 11 + PowerShell 5.1 |
| 网络 | 可访问模型 API | 走兼容代理时设 `DEEPSEEK_BASE_URL` |
| 磁盘 | ≥ 1 GB | `.venv` 约 785 MB（含 pyarrow/onnxruntime 等） |
| 系统依赖（可选） | LibreOffice、tesseract | 不装则分别少支持 `.doc/.ppt/.xls/.odt/.wps` 与扫描件 OCR |

系统依赖安装：

```powershell
winget install TheDocumentFoundation.LibreOffice   # Windows
# brew install --cask libreoffice                  # macOS
# sudo apt install libreoffice tesseract-ocr       # Debian/Ubuntu
```

用 `python -m docqa.cli engines` 或 `GET /api/engines` 可以看本机实际可用性。

### 4.2 安装

```powershell
cd D:\project\Harness\docqa

python -m venv .venv
.\.venv\Scripts\Activate.ps1

# SDK 目前只有预发布版本，必须带 --pre；否则 pip 不会选中 0.1.5rc1
$env:PYTHONUTF8 = "1"          # 关键：requirements.txt 含中文注释，GBK 代码页下 pip 会解码失败
python -m pip install --pre -r requirements.txt
```

> **受限环境的两个额外开关。** 如果在一个只允许写工作区的沙箱里安装/运行（例如从 DSH 会话里启动），
> 需要把临时目录与原生模块缓存都指向工作区内，否则 pip 会卡在 `tempfile` 上、runtime 会以
> `EPERM: mkdir ~/.cache/pkg/...` 启动失败：
>
> ```powershell
> $tmp = "$PWD\.tmp-run"; New-Item -ItemType Directory -Force $tmp | Out-Null
> $env:TMP = $tmp; $env:TEMP = $tmp; $env:TMPDIR = $tmp
> $env:PKG_NATIVE_CACHE_PATH = "$PWD\.docqa-web\.cache\pkg-native"
> python -m pip install --pre --no-cache-dir -r requirements.txt
> ```

### 4.3 配置与凭据：项目根目录的 `.env`

`run_web.py` / `docqa.cli` / Streamlit 三个入口都会在读取任何配置**之前**加载项目根目录的 `.env`
（`docqa/envfile.py`）。优先级从高到低：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 命令行参数 | 显式传入的永远赢（`--library` 会盖掉 `.env` 里的 `DOCQA_LIBRARY`） |
| 2 | 真实环境变量 | shell 里已 export 的 |
| 3 | **项目根 `.env`** | 本机制；`--env-file <path>` 可换路径，`--no-env-file` 可整体关闭 |
| 4 | `~/.dsh/.credentials.yaml` | `refs.DEEPSEEK_API_KEY`；只补 API key（`--credentials` 可改路径，需要 `pyyaml`） |
| 5 | 内置默认值 | 见 4.5 |

`.env` 只**补**配置、从不覆盖已有变量，所以它不会破坏启动它的 shell 环境（PATH、虚拟环境、代理一律以真实环境为准）。
启动日志会打印**实际生效的键名**和凭据**来源**，但从不打印值：

```
[docqa] .env      D:\…\docqa\.env（生效 3 项：DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DSH_MODEL）
[docqa] 凭据      DEEPSEEK_API_KEY ← .env D:\…\docqa\.env（长度 36，未打印）
[docqa] 权限      danger-full-access（agent 不再受写围栏约束，也不再弹审批；…）
[docqa] 接口     https://your-gateway.example/v1
```

仓库里带了一份模板 `​.env.example`，复制即可：

```powershell
Copy-Item .env.example .env
notepad .env          # 填 DEEPSEEK_API_KEY；Windows 打包运行时下还要取消
                      # DOCQA_PERMISSION_MODE=danger-full-access 的注释（见 4.4 / 4.6）
python run_web.py
```

`.env` 常用键：

| 键 | 作用 |
|---|---|
| `DEEPSEEK_API_KEY` | 模型凭据（等价于环境变量；也支持放托管凭据文件） |
| `DEEPSEEK_BASE_URL` | 兼容代理 / 自建网关；留空用 DeepSeek 官方地址 |
| `DSH_MODEL`（或 `DEEPSEEK_MODEL`） | 模型 id，必须是你 `settings.yaml` 里**存在**的，例如 `deepseek-flash` |
| `DOCQA_LIBRARY` | 资料库目录（默认 `.docqa-web`） |
| `DOCQA_PROFILE` | dsh profile（默认 `sdk`） |
| `DOCQA_PERMISSION_MODE` | dsh 权限预设；**Windows 打包运行时下必须 `danger-full-access`**（见 4.4） |
| `DOCQA_INLINE_BUDGET` | 全文内联的字符上限（默认 60000） |
| `DOCQA_MAX_IMAGES` / `DOCQA_IMAGE_MAX_BYTES` | 每轮内联图片的张数与单张字节上限（默认 4 张 / 8 MiB） |
| `DOCQA_DSH_BIN` / `DOCQA_INIT_TIMEOUT` / `DOCQA_GENUI_ASSETS` | 见 4.5 与 4.8 |

> **为什么必须由宿主来读项目根的 `.env`？** dsh 运行时自己也有 `.env` 分层（`<$DSH_HOME>/.env`
> 与它自己 cwd 下的 `.env`，本项目中分别是 `<library>/home/.env` 和 `<library>/workspace/.env`），
> 但**项目根目录不在其中**。另外把密钥放进 `<library>/workspace/` 是有风险的：那是 agent 的工作目录，
> 它可以用 `read` 工具看到。项目根目录在 agent 沙箱之外，反而更安全。`.env` 里有密钥，不要提交到版本库。

### 4.4 启动

> **务必从普通终端启动。** 若在 agent 会话的 shell 里（后台任务也算）拉起 `run_web.py`，
> 整个进程树都带着受限令牌：docqa 自己的 dsh 运行时再套一层沙箱时会失败，
> 表现为 `SetNamedSecurityInfoW failed (Win32 5): grantWrite(<library>\workspace)`，
> **所有** shell 与检索类工具在初始化阶段就挂掉。详见「四个环境坑」第 2 条。

Windows + pip 装的打包运行时（`deepseek-harness-sdk-runtime-win-x64.exe`）下，**权限模式是必填项**：
那条路径上的 Windows ACL runner 起不来，只有 `danger-full-access` 能绕开它（原因见
「Windows 沙箱 runner」）。两种等价写法，选一个即可：

```powershell
# 写法一：命令行（推荐，最不容易忘）
python run_web.py --port 8077 --permission-mode danger-full-access

# 写法二：写进 .env，之后所有入口（Web / CLI / Streamlit）都生效
#   DOCQA_PERMISSION_MODE=danger-full-access
python run_web.py --port 8077

# 其它组合
python run_web.py --port 8077 --library D:\data\docqa --model deepseek-flash --inline-budget 60000 --permission-mode danger-full-access

# 用另一份配置
python run_web.py --env-file D:\config\docqa-prod.env
```

启动日志必须出现这一行，否则 shell/检索不可用：

```
[docqa] 权限      danger-full-access（agent 不再受写围栏约束，也不再弹审批；…）
```

不想放宽权限就走 **B：换 node 版 dsh**（`--dsh-bin`，见 4.8 与「Windows 沙箱 runner」），
那时 `workspace-write` 围栏可以保留——因为 `process.execPath` 是真正的 node，
ACL runner 能被正常拉起。

打开 <http://127.0.0.1:8077>：左下角 **＋ → 文件** 选本地文件（或把文件拖到窗口任意位置）→
文件先挂在输入框上方 → 输入问题 → Enter / 发送，附件与问题一起入库并进入对话。

命令行入口（不起 Web，同样受权限模式影响，建议带上同一个开关）：

```powershell
python -m docqa.cli --library .docqa-web --permission-mode danger-full-access ingest .\手册.pdf .\预算.xlsx
python -m docqa.cli --library .docqa-web --permission-mode danger-full-access ask "预算里云资源占比多少？"
python -m docqa.cli --library .docqa-web status / engines / reset    # 状态 / 引擎可用性 / 清空资料库
```

> **CLI 也会读项目根的 `.env`**（同一个加载器），模型与权限默认值与 Web 一致。
> 唯一的差别是**资料库目录**：CLI 默认 `./.docqa`，Web 默认 `./.docqa-web`。要操作同一份资料库就显式传
> `--library .docqa-web`。

Streamlit 备用界面：`streamlit run docqa\app.py`（权限模式同样从 `.env` 读）。

### 4.5 参数与环境变量参考

`run_web.py` 的命令行参数（括号内为等价环境变量）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | `127.0.0.1` / `8077` | 监听地址与端口 |
| `--library` | `.docqa-web`（`DOCQA_LIBRARY`） | 资料库根目录 |
| `--model` | `deepseek-flash`（`DSH_MODEL`） | 模型 id |
| `--profile` | `sdk`（`DOCQA_PROFILE`） | dsh profile；`sdk-minimal` 不够用 |
| `--inline-budget` | `60000`（`DOCQA_INLINE_BUDGET`） | 全文内联的字符上限，超过则改走 agent 检索 |
| `--permission-mode` | 跟随 dsh（`workspace-write`） | dsh 权限预设；Windows 打包运行时下 shell/检索需要 `danger-full-access`（见「Windows 沙箱 runner」） |
| `--dsh-bin` | 空（`DOCQA_DSH_BIN`） | 指定 `dsh` 可执行文件；不设则用 pip 装的 `deepseek-harness-runtime-bin` |
| `--sdk-src` | 本机 checkout 路径 | 把 SDK 源码目录插到 `sys.path` 最前（未 pip 装 SDK 时用） |
| `--credentials` | `~/.dsh/.credentials.yaml` | 托管凭据文件路径 |
| `--env-file` | `<项目根>/.env`（`DOCQA_ENV_FILE`） | dotenv 文件路径 |
| `--no-env-file` | 关 | 完全不加载 `.env` |
| `--no-stream` | 关 | 不挂 token 流式转发插件（回答退化为按步整段出现） |
| `--no-genui` | 关 | 不挂 dsh-genui 宿主半边（模型不再主动输出围栏） |
| `--genui-assets` | 空（`DOCQA_GENUI_ASSETS`） | dsh-genui 的 `lib/assets` 目录（mermaid.js / three.js） |
| `--reload` | 关 | uvicorn 热重载（仅开发用） |

进程内还会用到（由 `run_web.py` 自动设置，一般不用手改）：
`DOCQA_PATCHES`（两层 `--patch` 文件列表）、`DOCQA_STREAM_HOOK` / `DOCQA_STREAM_TOKEN`（转发插件回调地址与令牌）、
`DOCQA_INIT_TIMEOUT`（runtime 初始化超时，默认 180s）、`PKG_NATIVE_CACHE_PATH`（原生模块缓存目录）。

可以放进 `.env` 的图片通道开关：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DOCQA_MAX_IMAGES` | `4` | 每轮最多内联几张图片；超出的只在 prompt 里标注「未附上」 |
| `DOCQA_IMAGE_MAX_BYTES` | `8388608`（8 MiB） | 单张图片的字节上限；超出的不内联并注明原因 |
| `DOCQA_PERMISSION_MODE` | 空（跟随 dsh） | `read-only` / `workspace-write` / `danger-full-access`；DSH 自己的 `DSH_PERMISSION_MODE` 同样识别 |

### 4.6 部署后验证

```powershell
curl http://127.0.0.1:8077/api/health      # {"ok":true,"documents":N,"session_id":"docqa-…"}
curl http://127.0.0.1:8077/api/engines     # 三层转换链可用性（首次调用会冷导入，可能要几秒）
```

启动日志里应能看到这些关键信息（`.env`、凭据、权限三行只打印键名/来源/模式，不含值）：

```
[docqa] .env      D:\…\docqa\.env（生效 3 项：DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DSH_MODEL）
[docqa] 凭据      DEEPSEEK_API_KEY ← .env D:\…\docqa\.env（长度 36，未打印）
[docqa] 权限      danger-full-access（agent 不再受写围栏约束，也不再弹审批；…）
[docqa] 流式转发  已挂载 …\docqa\plugin\cordis.patch.yml
[docqa] GenUI     已挂载 …\docqa\plugin\genui.patch.yml
[docqa] GenUI 资产 C:\Users\<你>\.dsh\profiles\web\node_modules\@omdsh-dev\dsh-genui\lib\assets
[docqa] 模型     deepseek-flash / profile=sdk
[docqa] 接口     DeepSeek 官方默认          # 或你 DEEPSEEK_BASE_URL 指定的地址
```

`/api/health` 只证明进程活着；**真正要验的是工具链**。三项冒烟（都在网页里问一句即可）：

| 冒烟项 | 怎么问 | 通过的样子 |
|---|---|---|
| shell | 「用 pwsh 执行 `Get-Location` 并原样报告输出」 | 返回工作目录路径；**不是** `error: --profile <name> is required` |
| 检索 | 「用 glob 找出工作区里所有 `.png`」 | 列出 `uploads\…png`；**不是** `ripgrep launch failed` |
| 图片 | 上传一张图后问「图中描述了啥」 | 阶段提示出现「思考中 · 已附上 1 张图片」，答案描述的是图的内容 |

前两项失败 = 权限模式没生效（去看启动日志的 `[docqa] 权限` 那行）；第三项失败 = 模型不支持图片输入。

### 4.7 生产化建议

- **必须单 worker、单实例。** 作业注册表与 SSE 事件缓冲都在进程内存里；`uvicorn --workers N`
  或多副本会让「POST 拿到 job_id 的进程」和「GET SSE 的进程」不是同一个，直接 404。
- **权限模式要显式记录运维决策。** Windows 打包运行时下默认路线是 `danger-full-access`
  （写围栏与审批都关掉，agent 可写进程可达的任意路径）；若你的部署要求保留围栏，就必须切到
  **B（node 版 `dsh`）**，否则 shell/检索不可用。两者选哪个应当写在部署单上，而不是靠默认值。
- **反向代理要关缓冲**，否则 SSE 会被攒成一坨：Nginx 用 `proxy_buffering off; proxy_read_timeout 3600s;`
  （应用已显式返回 `X-Accel-Buffering: no`）。代理层不要重写或压缩 `text/event-stream`。
- **重启等于新会话**：runtime 换了进程就无法沿用旧 session id，引擎会轮换出新 id（文档与资料库保留，
  但之前的对话轮次不再作为上下文）。
- **备份就是备份目录**：把 `--library` 指向的目录整体备份即可（`state.json` 丢了也能从 `workspace/` 重建索引）。
- **健康检查**用 `GET /api/health`；它不触发模型调用。
- **日志**：`run_web.py` 自身不写文件，交给外层（`tee`、systemd journal、NSSM 重定向）即可。
  若自己重定向，建议留一份（本仓库验证时用的是 `.docqa-web\server.out.log` / `server.err.log`）。

### 4.8 离线 / 受限网络部署

1. **不 pip 装 SDK**：`--sdk-src <checkout>\python\sdk\src` 走源码路径，`--dsh-bin <本地构建的 dsh>` 指定运行时。
   SDK 的 Python 侧很薄（除 `pydantic` 外无重依赖），源码路径同样可用。
   **顺带好处**：这条路径上的 `dsh` 是 node 版（`dsh.CMD` / `dsh.js`），
   `process.execPath` 是真正的 node，Windows ACL runner 能正常启动——
   也就是说 **B 路线天然免掉了 `danger-full-access`**，`workspace-write` 围栏可以保留（见「Windows 沙箱 runner」）。
2. **GenUI 资产**：`docqa/plugin/genui/` 已把宿主半边 vendor 进仓库（MIT，自包含，不依赖任何 `@deepseek-ai/*`）；
   只有 mermaid / three 两个大文件按需从 `--genui-assets` 提供，缺了只是这两种组件不可用。
3. **彻底断网**：`--no-stream --no-genui` 可关掉两层插件（回答退回「按步整段 + 纯 Markdown」）。
4. **依赖体积**：`pyarrow` / `onnxruntime` 来自 `streamlit` 与 `markitdown`。不用 Streamlit、不需要
   MarkItDown 覆盖面的场景，可以从 `requirements.txt` 里去掉这两条以显著瘦身。

### 4.9 清理

```powershell
# 只清缓存与产物，保留资料库（原文与转换结果）
Remove-Item -Recurse -Force .docqa-web\.cache

# 清空资料库（文档没了，SDK home 与凭据保留）
python -m docqa.cli reset

# 完全卸载
Remove-Item -Recurse -Force .venv, .docqa-web
```

`.env` 是你自己的配置（含密钥），不属于"垃圾"；`.env.example` 是模板，两者都别删。

---

## 五、架构与设计

### 为什么不做向量库

个人文档问答的语料通常是几个到几十个文件，几百 KB 到几 MB。这个量级下：

```
上传 → 宿主落盘原文 + 转成 Markdown → 按体量二选一
                                      ├─ 小（≤ inline_budget）：全文直接进 prompt，一次答完
                                      └─ 大：只给 INDEX.md，agent 自己 grep 定位 + read 取上下文
```

- 小语料内联：省掉检索环节，没有召回率问题，首字延迟最低。
- 大语料走 agent 检索：DSH 自带 `tool-result-pruner`、`spill`、`compaction`，配合 `grep`/`read` 就够用；
  真正的瓶颈是模型步数，不是检索精度。
- 只有当语料大到 grep 也扫不动（几百上千文件、单文件几十万字符）时，才值得引入向量检索。

### 上下文管理（三层）

| 层 | 谁在管 | 机制 |
|---|---|---|
| 装配 | 本仓库 `engine._build_input()` | 每轮重新装配：系统纪律 + 文档清单 + （全文 \| 只给清单）+ 问题；`inline_budget` 是主开关 |
| 会话 | SDK 传输层 | 历史只存在于 runtime 的 session 里；重启即轮换新 session（见「已知限制」） |
| 窗口 | dsh runtime | `token-meter` 计量 + `compaction-basic`（压力 ≥ 窗口 × 0.8 时摘要旧区、保留最新 16%）+ 工具结果剪枝（>8192 字符留头 4096 + 尾 1024）+ 超限落盘（>50KB） |

实测账本（一次提问后 runtime 自己写的投影缓存）：上下文窗口 1,000,000 tokens、本次请求 prompt 12,196 tokens、
其中工具 schema 占 6,481、面上消息 5,129。**结论：这个量级下自动压缩几乎不会触发，真正的上下文开关是
`inline_budget`。**

### 三层格式转换链

「Office 全家桶 + PDF + Markdown」不是一个转换器能覆盖的，所以按**能拿到多少结构**分三层，逐层尝试，第一层成功就停：

| 层 | 引擎 | 覆盖 | 特点 |
|---|---|---|---|
| 1 原生精确解析 | `pypdf`→`pdfminer`、`python-docx`、`openpyxl`、`xlrd`、`python-pptx`、`csv`、`striprtf` | pdf、docx、xlsx/xlsm、xls、pptx、csv/tsv、rtf、md/txt/代码 | 保留标题层级与表格行列关系；无子进程，最快 |
| 2 LibreOffice 无头 | `soffice --headless --convert-to` | doc、ppt、xls、odt/ods/odp、wps/et/dps | 万能后备：先转成现代格式，**再回到第 1 层**拿结构，而不是拿扁平文本 |
| 3 MarkItDown | `markitdown` | epub、msg、ipynb、html/xml、zip 等漏网格式 | 覆盖面最广，结构信息较弱 |
| 兜底 | 纯文本嗅探 | 任何其实是文本却没被识别的文件 | 保证「至少能读到」 |

分派规则：

| 扩展名 | 走哪条路 | 产出 |
|---|---|---|
| `.md` `.markdown` `.txt` `.json` `.yaml` `.py` `.sql` … | 原样透传 | 不变 |
| `.csv` `.tsv` | 原生 csv（自动嗅探分隔符） | Markdown 表格 |
| `.docx` / `.doc` | python-docx；`.doc` 先经 LibreOffice | 标题层级 + 表格 |
| `.xlsx` `.xlsm` / `.xls` | openpyxl / xlrd（xlrd 2.x 只支持 `.xls`） | 每个工作表一节 + 表格 |
| `.pptx` / `.ppt` | python-pptx；`.ppt` 先经 LibreOffice | 每张幻灯片一节 + 表格 + 演讲者备注 |
| `.pdf` | pypdf，失败或异常时 pdfminer | 每页一节；文本密度过低标记为疑似扫描件 |
| `.html` `.htm` `.xml` | MarkItDown | 干净 Markdown |
| `.rtf` | striprtf | 纯文本 |
| `.odt` `.ods` `.odp` `.wps` `.et` `.dps` | LibreOffice | 同 docx/xlsx/pptx |
| `.epub` `.msg` `.ipynb` `.zip` … | MarkItDown | 尽力而为 |
| `.png` `.jpg` … | 不做 OCR，登记为图片条目 | 无 Markdown；走「图片通道」（见下） |

两个关键设计：

1. **按魔数嗅探，不信扩展名。** 文件头是 `%PDF`、`PK\x03\x04`、OLE2 `D0CF11E0`、`{\rtf` 就按真实格式走；
   ZIP 容器再看内部是 `word/`、`xl/`、`ppt/` 还是 ODF；OLE2 用 `olefile` 看 `WordDocument`/`Workbook`/
   `PowerPoint Document` 流区分 doc/xls/ppt。所以改名文件也能正确转换。
2. **LibreOffice 的 profile 必须重定向**（`-env:UserInstallation=file:///<library>/.cache/loprofile`），
   否则 `soffice` 往系统 AppData 写配置，在受限环境里直接失败。

每条转换结果都记录**实际命中的引擎**（`DocEntry.engine`），写进 `INDEX.md`，出问题一眼看出是哪一层做的。

### 图片走视觉通道

图片是**唯一不靠「工作目录里的文件」**的东西：没有 OCR 就没有 Markdown 全文，而 SDK 协议允许栅格图片块，
所以图片直接内联进那一轮的消息：

```
上传 .png ──▶ uploads/<sha8>-name.png（原文，无 Markdown 条目）
提问     ──▶ /api/chat {question, images:[<图片 slug>]}
             └─ engine._build_input() 组装：[文本 prompt] + [image 块] + …
                  └─ prompt 里的「随本轮问题送来的图片」小节逐张点名，并声明 docs/ 下没有同名文件
```

- **默认自动带上**：`engine.library_images()` 把资料库里已登记（按登记时间倒序）的图片也一并内联，
  所以「上传 → 再提问」这种跨轮用法不需要重新挂一次附件——纯图片资料库因此终于可问。
- **上限**：`DOCQA_MAX_IMAGES`（默认 4 张）、`DOCQA_IMAGE_MAX_BYTES`（默认 8 MiB/张）。
  超出的图片**不静默丢弃**：prompt 的图片小节会写「未附上：…（原因）」，让模型如实告诉用户，
  而不是猜内容。
- **清单里有原文路径**：`| 原始文件 | Markdown 路径 | 原文路径 | 页数/备注 |`，
  所以真要用工具时 `read_image uploads/<sha8>-name.png` 也能读；但正常情况下不需要，
  也不依赖 `grep`/`glob`/`pwsh`（见「四个环境坑」第 2 条：嵌套沙箱会让这些工具失效）。
- **需要视觉模型**：模型不支持图片输入时，图片块会被适配器拒绝——那时仍可退回安装 tesseract 做 OCR。

### Windows 沙箱 runner（shell 与检索为什么需要 danger-full-access）

`profile=sdk` 会挂上完整的工具集（`pwsh`、`grep`、`glob`、`read`…），所以工具**存在**；
麻烦在它们**跑不起来**的地方——执行前要过一层 Windows ACL 沙箱：

```
pwsh / grep / glob ──▶ ctx.sandbox（sandbox-local）
                        └─ Windows ACL runner 的启动形式是 [process.execPath, <runner 入口>]
                             └─ 打包运行时里 process.execPath 是自包含 exe
                                （deepseek-harness-sdk-runtime-win-x64.exe，约 240 MB SEA）
                                → runner 入口在 SEA 里解析不到 → 退化成「拿 dsh 自己当 runner」
                                → CLI 报 error: --profile <name> is required
```

实测症状：`pwsh` 的任何命令（连 `echo hello` 也是）都在执行前返回
`stdout: (空) / stderr: error: --profile <name> is required / exit code 1`，
`grep`/`glob` 则是 `ripgrep launch failed`——它们都走同一个 runner。
这**不是** profile 的问题（换 `sdk`/`sdk-minimal` 都绕不过 runner 组装），也**不是** docqa 代码的问题。

三个可选出路：

| 出路 | 做法 | 代价 |
|---|---|---|
| **A 权限预设（默认推荐）** | `--permission-mode danger-full-access` 或 `.env` 写 `DOCQA_PERMISSION_MODE=danger-full-access` | 用不到沙箱 runner，shell/检索立刻可用；但 agent 写操作不再被围栏限制，审批策略同时降为 `never` |
| B 换 node 版 dsh | `DOCQA_DSH_BIN` 指向基于 node 的 `dsh` 启动器（如 checkout 里的入口） | 保留 `workspace-write` 围栏；需要本机有可用的 node 版运行时，冷启动更慢 |
| C 上游修 | 让 SEA 运行时能找到/托管 ACL runner 入口 | 只能等 DSH 打包层修，期间 Windows 上 shell/检索始终不可用 |

`DOCQA_PERMISSION_MODE` 是 DSH 官方留的部署开关（base 的 `sandbox-policy.mode` 与 approval
policy 都读 `DSH_PERMISSION_MODE`），DSH 自己的 SDK/headless 测试也用同一个开关把运行时切到
`danger-full-access`，所以这不是绕过机制，而是预期用法。

对照：`sdk-minimal` 把 `mode: danger-full-access` 写死在自己的 patch 里，因此它的 shell 天然可用——
代价是它刻意砍掉了 `read`/`grep`/fs 工具、compaction 与 skills，docqa 的提示词会直接失效，
所以不要用 `sdk-minimal` 来"顺便"解决这个问题（见「已知限制」第 5 条）。

---

## 六、Web 界面与 REST API

```
浏览器 ──HTTP──▶ FastAPI (docqa/server.py) ──▶ DocQAEngine ──stdio JSON-RPC──▶ dsh --profile sdk
   │                    │
   └──SSE───────────────┘  GET /api/chat/{job_id}/events
```

用 SSE 而不是 WebSocket：流量本来就是单向的（服务端 → 浏览器），SSE 走普通 HTTP、自带断线重连
（`Last-Event-ID` 补发），丢掉一轮也不需要重新握手。

### REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/state` | 会话 id、模型、内联预算、文档清单（含每个文件命中的转换引擎） |
| `GET` | `/api/engines` | 三层转换链在本机的可用性（首次调用冷导入，可能几秒） |
| `POST` | `/api/documents` | multipart 多文件上传 → 落盘 + 转换，返回每个文件的引擎与字符数（图片额外回传 `slug` / `upload` / `image`） |
| `DELETE` | `/api/documents` | 清空资料库（注意：不写回 `state.json` 的文档索引，重启后会读回旧索引，见「已知限制」） |
| `POST` | `/api/session` | 轮换会话 id（文档保留） |
| `POST` | `/api/chat` | `{question, images?:[slug]}` → **202** + `job_id`；`images` 只接受资料库内已登记的图片文件名，上一轮未结束返回 **409** |
| `GET` | `/api/chat/{job_id}` | 轮次状态快照 |
| `GET` | `/api/chat/{job_id}/events` | **SSE 事件流**，支持 `Last-Event-ID` 断线补发 |
| `POST` | `/api/genui-action` | GenUI 组件动作 → 变成本会话的下一轮提问（202 + `job_id`） |
| `GET` | `/plugin-assets/{name}` | 提供 `mermaid.js` / `three.js`（来自 dsh-genui 安装目录） |
| `GET` | `/` | 单文件前端 |
| `GET` | `/api/health` | 健康检查（不触发模型调用） |

> `POST /api/agent-stream` 是**内部端点**：只给 runtime 内的转发插件回传 token 增量，需要
> 进程级的 `x-docqa-token`，不是给外部调用的 API。

`POST /api/chat` 立即返回而不阻塞是本设计的要点：SSE 是独立的一次 GET，所以浏览器可以在同一轮里
既拿到 202 又持续接收事件。

### SSE 事件类型

契约定义在 `docqa/events.py`，前端按类型分别渲染：

| 事件 | 前端渲染 | 实时性 |
|---|---|---|
| `delta` | 流式正文（带光标）与思考行增量 | **真实时**（token 级，经插件转发） |
| `status` | 顶部状态行（连接中 / 思考中 / 完成） | **实时** |
| `turn_start` / `step_start` / `step_end` / `turn_end` | 状态行与结束态 | **实时** |
| `tool_call` | 事件行「动词 · 主参数」，点开看参数 | **实时**（执行前落库） |
| `tool_result` | 该行收尾：完成/失败 + 耗时 + 可展开结果 | **实时**（执行后落库） |
| `thinking` | 思考事件行（一行摘要，点开看全文） | **按步**（该步提交时整段到达） |
| `text` | 正文（Markdown），并取代同一步的流式缓冲 | **按步** |
| `done` | 最终回答（Markdown），提升为 `.answer` | 轮末 |
| `error` | 红色错误框 | 轮末 |

### 前端渲染

`docqa/static/index.html` 单文件、零外链、无构建步骤：

- **事件行**：每个事件一条 `.ev` 行（`💭 思考`、`搜索 · 备份|回滚`、`读取 · docs/xx.md` + `完成 · 10 ms`），
  直接铺在对话流里，可点击展开细节——与 DSH 的对话 UI 同构。
- **附件暂存**：选中的文件先挂在输入框上方（可单个移除），按 Enter / 发送才入库并进入对话。
- **token 流式**：`delta` 走独立监听（不参与 `seq` 去重），正文用 rAF 节流重渲染 Markdown；
  提交事件到达时丢弃流式块、改用权威整段，因此不会重复也不会因半截 JSON 抖动。
- **GenUI 渲染器**：` ```dsh-ui ` 围栏 → 组件树（见下节）。
- **Markdown**：自带渲染器（标题/列表/表格/代码块/引用/行内样式），不引 CDN。
- 断线重连按 `seq` 去重，不会重复渲染。

---

## 七、token 级流式是怎么实现的

SDK 的通知面只有 4 种（`session.event` / `session.status` / `subagent.started` / `subagent.finished`），
**没有 token 级 chunk**——`assistant/message` 只在一步结束时提交。但 token 增量在 runtime 进程内是存在的：
`agent/assistant-stream` 事件携带 `text-delta` / `reasoning-delta` 帧（DSH 自己的 Web UI 就是靠它做流式的）。

docqa 补上了这段通道：

```
dsh runtime 进程                                   宿主进程（FastAPI）
  agent/assistant-stream ──▶ docqa-stream-forward ──POST /api/agent-stream──▶ JobRegistry
   （token 帧）                 （订阅 + 40ms 攒批）      （带 x-docqa-token）      │
                                                                                ▼
                                         浏览器 ◀──SSE event: delta── 在线订阅者（不进重放缓冲）
```

- 插件通过 SDK 的 `--patch` 机制叠在 `sdk` profile 上（`docqa/plugin/cordis.patch.yml`），
  没设置 `DOCQA_STREAM_HOOK` 就完全不动作；任何异常与网络失败都被吞掉，不影响 agent 主流程。
- 增量是**暂态**的：不写入作业的重放缓冲（权威文本以提交的整段为准），`--no-stream` 可整体关闭。

**文字粒度的边界**：流式块在**步末**会被提交事件替换——这是刻意的，保证 Markdown 与最终回答完全一致。
所以"边想边说"的粒度是 token，但"落定"的粒度仍是步。

---

## 八、GenUI：结构化界面

模型被教会用 ` ```dsh-ui ` 围栏输出 JSON 组件树，前端渲染成真实组件（表格 / 图表 / 指标卡 / 步骤 / quiz /
mermaid / 3D 等）。能力来自 `@omdsh-dev/dsh-genui`（v0.8.3，MIT），但**只复用了它的宿主半边**：

| 部分 | 落地方式 |
|---|---|
| 宿主半边 | vendor 成 `docqa/plugin/genui/index.js`（53 KB，只依赖 Node 内置），经 `genui.patch.yml` 叠到 sdk profile：注入围栏规范的**系统提示词**、注册 `validate_dsh_ui`（校验 + 自动修复 JSON）与 `render_ui` 工具 |
| 浏览器半边 | 插件的 React 渲染器依赖 DSH 客户端运行时（`__ModuleLoader__` / slots / sessions），docqa 是单文件 vanilla 前端，因此**自己实现**了 vanilla 渲染器（`docqa/static/index.html` 内 `GENUI` 段） |
| 大资产 | `/plugin-assets/{mermaid.js,three.js}` 直接服务插件安装目录里的 IIFE（3.3 MB / 687 KB，按需懒加载，不拷贝进仓库） |
| 动作回路 | 组件点 → `[genui-action] <name>` → `POST /api/genui-action` → 同一 session 新一轮 → 复用全套 SSE/流式渲染 |

已实现的组件：`text` `button` `input` `textarea` `select` `checkbox` `radio` `switch` `slider` `link`
`badge` `stat` `progress` `divider` `avatar` `spacer` `row` `col` `grid` `card` `list` `table` `chart`
`tabs` `plot` `callout` `steps` `keyvalue` `diff` `json` `code` `accordion` `copy` `mermaid` `scene3d`
`timeline` `file-tree` `breadcrumb` `quiz` `submit`。

与上游插件的差异（刻意的简化）：没有 `panel:true` 的停靠面板与 `/panel` 命令；`plot` 支持表达式文法与
参数滑杆，但没有拖拽平移 / 滚轮缩放 / 动画播放；`chart` 没有 `series` 分组柱；`scene3d` 没有动画播放。
另：围栏 JSON 带容错修复（裸引号、尾逗号、未闭合括号），限额与上游一致（深度 8 / 节点 200 / 单字符串 2000）。

---

## 九、已知限制

1. **文字"落定"按步。** token 级流式已实现，但思考与正文的提交事件仍是按步到达，步末会用权威整段替换流式块。
2. **跨进程不能续接会话。** `ctx.agents.create()` 对已存在的 session id 直接抛错，而 SDK 服务端只调 `create`、
   从不调 `resume`；引擎用 `runtime_token` 检测并轮换新 id（旧 id 保留在 `previous_session_ids`，最多 5 个），
   `ask()` 里还有一层 `already exists` 自愈。**代价：重启服务后对话上下文从零开始，文档与资料库不受影响。**
3. **首轮启动慢。** 首次要生成 profile、拉起 `dsh` 子进程、解包原生模块（约 19.5 MB），
   冷启动实测约 10–18 s；`initialize_timeout_seconds` 默认 180 s。
4. **图片不做 OCR，只走视觉通道。** 上传的图片登记后没有 Markdown 全文，靠图像块随问题内联（见
   「图片走视觉通道」）。这要求模型支持图片输入；不支持时图片块会被适配器拒绝，此时只能装 tesseract 走 OCR。
   另外内联有上限（默认 4 张 / 8 MiB），超出的图片会在 prompt 里标「未附上」，不会被悄悄丢掉。
5. **`sdk-minimal` profile 不够用。** 它只有一个持久 shell，没有 `read`/`grep`/compaction/skills，
   不适合文档问答；它把权限写死成 `danger-full-access` 只是其简约配置树的副产物，不要为了
   "让 shell 能用"而换过去（正确做法见「Windows 沙箱 runner」的 A）。
6. **越权审批关闭式失败。** agent 若试图访问工作目录之外，因无人应答而失败——这是预期行为。
   注意：选了 `danger-full-access`（Windows 打包运行时的默认路线）后这条不再适用——
   审批策略是 `never`，agent 的写操作也不会被围栏限制，见第 11 条。
7. **`DELETE /api/documents` 不回写索引。** 清空后 `state.json` 仍留着旧文档条目，重启后会读回「幽灵文档」；
   需要彻底复位时手动删掉 `state.json`。
8. **单进程串行。** 同时只允许一轮（`POST /api/chat` 在忙时返回 409），事件与作业都在内存里，不能多 worker。
9. **预发布版本。** SDK 目前是 `0.1.5rc1`，API 可能变动；`RunResult`、`DeepSeekHarnessConfig` 是稳定入口，
   `_launch_args` 之类的内部适配器明确不属于公开 API。
10. **GenUI 是 vanilla 重写版**，视觉与上游 React 版不会逐像素一致（配色跟随 docqa 主题）；上游升级时
    宿主半边需重新 vendor（版本记在 `docqa/plugin/genui/UPSTREAM-package.json`）。
11. **Windows 打包运行时下，`danger-full-access` 是 shell/检索可用的前提（默认路线）。**
    它同时把审批降为 `never`：agent 的写操作不再被围栏限制在资料库工作区，能写进程可达的任意路径。
    这是 DSH 打包层（SEA 运行时起不了 ACL runner）逼出来的取舍，不是 docqa 的选择；
    要恢复围栏就必须换 node 版 `dsh`（B 路线）。详见「Windows 沙箱 runner」。

---

## 十、开发与验证记录

以下结论来自开发期实测；过程中用过的测试脚本与样例语料已清理，保留结论与踩坑作为复现参考。

### 转换链与离线自测

- 转换与 prompt 装配自测 26 项全绿：转换、原文逐字节保留、幂等去重、两条 prompt 装配路径、
  `state.json` 丢失后从工作区重建索引、新 runtime 的 session 轮换。
- 格式覆盖 21 项全绿（覆盖三层）：`.md` `.csv` `.docx` `.xlsx` `.pptx` `.rtf` 结构完整（含 DOCX 表格、
  PPTX 表格与备注）；`.pdf` 走 pypdf；`.html` `.epub` 走 markitdown；`.xls` 走 xlrd；
  `.doc` `.ppt` `.odt` 走 LibreOffice→原生；无文本层 PDF 正确标记为疑似扫描件；
  把 `.doc` 改名 `.docx`、`.docx` 改名 `.pdf` 也按真实格式处理。
- 一个诚实的边界：测试里的 `.doc` 样本由 LibreOffice 的 Word 97 **导出**过滤器生成，而该过滤器会把表格
  压平成一串文本，所以 `.doc` 只断言了单元格文字存活、没断言表格结构（读取侧走的是导入过滤器，不受影响）。
- 图片通道回归测试：`python tests\test_image_channel.py`（30 项断言，纯离线、不调用模型、不启动 runtime）。
  用临时资料库 + 1×1 PNG + 桩引擎覆盖：图像块内联与字节还原、prompt 图片小节与清单「原文路径」、
  `max_images` / `max_image_bytes` 的「未附上」说明、slug 解析拒绝路径穿越、`/api/documents` 回传
  `slug`/`upload`/`image`、`/api/chat` 把图片一路传到 `engine.ask_stream()`。
- 权限预设回归测试：`python tests\test_permission_mode.py`（14 项断言，拦截 `DeepSeekHarness` 构造参数，
  不启动 runtime）。覆盖：预设校验与优先级、默认不改 `DSH_PERMISSION_MODE`、显式开启后把
  `danger-full-access` / `read-only` 传进运行时子进程 env、凭据与 base_url 不受影响、
  `server.default_engine()` 同时识别 `DOCQA_PERMISSION_MODE` 与 `DSH_PERMISSION_MODE`。

### 端到端真实问答

语料为 5 种格式的混合文件集：

| 路径 | 配置 | 结果 |
|---|---|---|
| 小语料内联 | `inline_budget=60000` | 4 轮全部 `completed`，每轮仅 1 个模型步，答案精确到「文件名 + 表格列名」，并主动声明「文档中未提及」 |
| 大语料工具检索 | `inline_budget=0` | agent 真实调用 `grep`/`glob`/`pwsh`/`read`，先定位再取上下文，2 轮同样正确；第 1 轮 5 个模型步、第 2 轮 2 步 |

### Web、流式与 GenUI

- REST + SSE 离线桩 20 项全绿：状态码语义、事件分帧、顺序、`seq` 自增、`tool_call`→`tool_result` 关联、
  `Last-Event-ID` 只补发后续、未知 job 404、空资料库 409、并发 409。
- 真浏览器（无头 Chrome）实测：事件行按类型渲染（`💭 思考` / `搜索 · 备份|回滚|灰度|窗口` /
  `读取 · docs/<sha8>-ops-test.md` + `完成 · 10 ms`）；中途采样 `busy=true`、结束时 5 行事件 + 完整回答。
- token 流式实测（高频采样）：t+0.7s 思考行已流入 86 字符 → t+1.4s 正文 328 字符 → t+2.1s 743 字符 →
  t+2.3s 提交版接管（866 字符，无重复）。
- GenUI 实测：模型先调 `validate_dsh_ui` 自检（2 ms）再输出围栏；围栏渲染出
  callout + 2 列 stat + 表格 + quiz + 动作按钮；点击按钮 → `[genui-action] refresh_board` →
  服务端收到并驱动新一轮 → 返回更新后的看板。mermaid 与 scene3d（canvas）均正常渲染。

### 四个环境坑（都已定位）

1. **系统代理会吞掉 localhost。** Windows 上 `urllib.request.getproxies()` 读注册表，若本机有
   `127.0.0.1:7890` 之类代理，httpx 连自己的服务也会被拦下。测试客户端用 `httpx.Client(trust_env=False)`，
   或设 `NO_PROXY=127.0.0.1,localhost`。
2. **嵌套沙箱会让 shell/搜索类工具失效。** 若宿主本身跑在沙箱里（例如由 agent 会话用后台任务启动
   `run_web.py`），SDK 运行时作为孙进程再套一层沙箱：ACL 授权（`grantWrite`）在受限令牌下拿不到
   `WRITE_DAC`，于是 `pwsh` → `SetNamedSecurityInfoW failed (Win32 5)`、`grep`/`glob` → `ripgrep launch failed`，
   只有 `read` 正常；模型会自适应退回 `read` 并答对文本类问题。
   证据与判据：`.docqa-web\workspace` 上会出现一条**非继承**的 capability ACE（授权成功）；
   **从普通终端启动 `run_web.py` 没有这个问题。** 纯图片问答也不受影响（图片随问题内联，不用工具）。
   受限环境需要的额外开关见 4.2。
3. **pip 与 runtime 的写盘位置。** pip 在 `tempfile` 上会卡住（临时目录被拒），runtime 会因
   `~/.cache/pkg` 不可写而 `EPERM` 启动失败——分别用工作区内的 `TMP`/`TEMP` 与
   `PKG_NATIVE_CACHE_PATH` 解决。
4. **打包运行时里 Windows ACL runner 起不来（与沙箱嵌套无关）。** 这是把第 2 条修好之后才露出来的下一层：
   runner 的启动形式是 `[process.execPath, <runner 入口>]`，而打包运行时的 `process.execPath` 是自包含 exe，
   于是 shell 与检索报 `error: --profile <name> is required`。从普通终端启动同样会遇到。
   解决路线（A/B/C）见「Windows 沙箱 runner」；本机当前用的是 A（`danger-full-access`）。

### 已修正的一个误判

早期根据 SDK 文档写过「同 `session_id` + 同 home 即跨进程续接持久对话」，**这是错的**，被真实报错推翻：
`JsonRpcError: session "docqa-…" already exists`。根因与处理见「已知限制」第 2 条。

---

## 附：从源码 checkout 运行（不 pip 装 SDK）

本机出站 TLS 受限时装不上 SDK，改走源码路径是可行的，也顺带证明了 SDK 的 Python 侧确实只是一层薄封装：

```powershell
python run_web.py `
  --sdk-src D:\project\Harness\deepseek-harness\python\sdk\src `
  --dsh-bin D:\project\Harness\deepseek-harness\python\sdk-runtime\node_modules\.bin\dsh.CMD `
  --model deepseek-flash
```

`run_web.py` 默认就会把本机 checkout 的 SDK 源码目录插到 `sys.path` 最前；不指定 `--dsh-bin` 时，
SDK 会使用 pip 安装的 `deepseek-harness-runtime-bin` 自带的运行时。

**这条路线不需要 `--permission-mode danger-full-access`。** `dsh.CMD` 拉起的是 node 版运行时，
`process.execPath` 是真正的 node，Windows ACL runner 能被正常启动，`workspace-write` 围栏因此得以保留——
也就是「Windows 沙箱 runner」里的 **B 路线**。代价是冷启动更慢，且依赖 checkout 已构建（`node_modules` 齐备）。
