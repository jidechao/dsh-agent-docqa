"""Launch the docqa web app.

    python run_web.py                       # http://127.0.0.1:8077
    python run_web.py --port 8080 --reload

配置来源（同名时上面的赢）：

1. 命令行参数（显式给出的）；
2. 真实环境变量（shell 里已 export 的）；
3. 项目根目录的 ``.env``（``--env-file`` 可换路径，``--no-env-file`` 可关）；
4. ``~/.dsh/.credentials.yaml`` 的 ``refs.DEEPSEEK_API_KEY``（只补 API key）；
5. 内置默认值。

``.env`` 里可以写任意配置，例如：

    DEEPSEEK_API_KEY=sk-…
    DEEPSEEK_BASE_URL=https://your-gateway.example/v1   # 兼容代理
    DSH_MODEL=deepseek-flash                           # 模型 id（DEEPSEEK_MODEL 亦可）
    DOCQA_LIBRARY=.docqa-web                           # 资料库目录
    DOCQA_PROFILE=sdk                                  # dsh profile
    DOCQA_INLINE_BUDGET=60000                           # 全文内联的字符上限

加载器永不覆盖已存在的变量，且只打印键名、绝不打印值。
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from docqa.engine import PERMISSION_MODES, resolve_permission_mode  # noqa: E402

DEFAULT_SDK_SOURCE = Path(r"D:\project\Harness\deepseek-harness\python\sdk\src")
DEFAULT_GENUI_ASSETS = Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / "@omdsh-dev" / "dsh-genui" / "lib" / "assets"

DEFAULT_LIBRARY = ".docqa-web"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_PROFILE = "sdk"
DEFAULT_INLINE_BUDGET = 60000


def _first_env(*names: str) -> str | None:
    """Return the first non-empty environment value among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="docqa web 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8077)
    # 配置项一律用 None 哨兵：先解析参数，再加载 .env，最后按优先级决议——
    # 这样 .env 里的值不会被 argparse 早在建 parser 时就定下的默认值挡住。
    parser.add_argument("--library", default=None, help=f"资料库目录（默认 {DEFAULT_LIBRARY}）")
    parser.add_argument("--model", default=None, help=f"模型 id（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--profile", default=None, help=f"dsh profile（默认 {DEFAULT_PROFILE}）")
    parser.add_argument("--dsh-bin", default=None, help="dsh 可执行文件（默认用 SDK 自带的运行时）")
    parser.add_argument("--inline-budget", type=int, default=None,
                        help=f"全文内联的字符上限（默认 {DEFAULT_INLINE_BUDGET}）")
    parser.add_argument("--permission-mode", default=None,
                        choices=list(PERMISSION_MODES),
                        help="dsh 权限预设（默认跟随 dsh：workspace-write；"
                             "Windows 打包运行时下 shell/检索需要 danger-full-access，见 README）")
    parser.add_argument("--sdk-src", default=str(DEFAULT_SDK_SOURCE),
                        help="SDK 源码目录（未 pip 安装时使用）")
    parser.add_argument("--credentials", default=str(Path.home() / ".dsh" / ".credentials.yaml"),
                        help="托管凭据文件；未设置 DEEPSEEK_API_KEY 时从这里读取（值只在进程内使用）")
    parser.add_argument("--env-file", default=None,
                        help="dotenv 文件路径（默认 <项目根>/.env；也可用 DOCQA_ENV_FILE 指定）")
    parser.add_argument("--no-env-file", action="store_true", help="完全不加载 .env")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--no-stream", action="store_true",
                        help="不挂 token 流式转发插件（回答会退化成按步整段出现）")
    parser.add_argument("--no-genui", action="store_true",
                        help="不挂 dsh-genui 宿主半边（模型不会主动输出 dsh-ui 围栏）")
    parser.add_argument("--genui-assets", default=None,
                        help="dsh-genui 的 lib/assets 目录（mermaid.js / three.js）")
    args = parser.parse_args()

    # ---------------------------------------------------------------- .env
    # 必须在任何 os.environ 读取之前完成：库目录/模型/profile 都从环境决议。
    key_from_env = os.environ.get("DEEPSEEK_API_KEY") or ""
    env_file = None
    if not args.no_env_file:
        candidate = args.env_file or os.environ.get("DOCQA_ENV_FILE") or str(HERE / ".env")
        env_file = Path(candidate).expanduser()
        from docqa.envfile import load_env_file

        applied = load_env_file(env_file)
        if applied:
            print(f"[docqa] .env      {env_file}（生效 {len(applied)} 项：{', '.join(sorted(applied))}）")
        elif env_file.is_file():
            print(f"[docqa] .env      {env_file}（没有新增：同名变量已在环境中）")
        else:
            print(f"[docqa] .env      未找到 {env_file}（跳过；--env-file 可指定，--no-env-file 可关闭）")

    # ------------------------------------------------------------ 配置决议
    library = args.library or _first_env("DOCQA_LIBRARY") or DEFAULT_LIBRARY
    model = args.model or _first_env("DSH_MODEL", "DEEPSEEK_MODEL", "DOCQA_MODEL") or DEFAULT_MODEL
    profile = args.profile or _first_env("DOCQA_PROFILE") or DEFAULT_PROFILE
    dsh_bin = args.dsh_bin or _first_env("DOCQA_DSH_BIN")
    genui_assets = args.genui_assets or _first_env("DOCQA_GENUI_ASSETS")
    if args.inline_budget is not None:
        inline_budget = args.inline_budget
    else:
        inline_budget = int(_first_env("DOCQA_INLINE_BUDGET") or DEFAULT_INLINE_BUDGET)
    try:
        permission_mode = (
            args.permission_mode
            if args.permission_mode is not None
            else resolve_permission_mode(
                os.environ.get("DOCQA_PERMISSION_MODE"), os.environ.get("DSH_PERMISSION_MODE")
            )
        )
    except ValueError as error:
        print(f"[docqa] 配置错误：{error}", file=sys.stderr)
        return 2

    # --------------------------------------------------------------- 凭据
    key_source = "环境变量" if key_from_env else ""
    if not os.environ.get("DEEPSEEK_API_KEY") and Path(args.credentials).exists():
        try:
            import yaml

            document = yaml.safe_load(Path(args.credentials).read_text(encoding="utf-8")) or {}
            key = (document.get("refs") or {}).get("DEEPSEEK_API_KEY")
            if isinstance(key, str) and key.strip():
                os.environ["DEEPSEEK_API_KEY"] = key.strip()
                key_source = f"托管凭据 {args.credentials}"
        except Exception as error:
            print(f"[docqa] 读取托管凭据失败：{type(error).__name__}: {error}", file=sys.stderr)
    if not key_source and os.environ.get("DEEPSEEK_API_KEY"):
        key_source = f".env {env_file}" if env_file is not None else ".env"
    key = os.environ.get("DEEPSEEK_API_KEY") or ""
    if key:
        print(f"[docqa] 凭据      DEEPSEEK_API_KEY ← {key_source}（长度 {len(key)}，未打印）")
    else:
        print("[docqa] 凭据      未配置 DEEPSEEK_API_KEY；模型调用会失败（.env 或环境变量任选其一）",
              file=sys.stderr)

    if args.sdk_src and Path(args.sdk_src).exists():
        sys.path.insert(0, args.sdk_src)
        print(f"[docqa] SDK 源码路径：{args.sdk_src}")

    try:
        import deepseek_harness  # noqa: F401
    except ImportError:
        print("[docqa] 警告：deepseek_harness 不可导入，问答会在首次请求时报错。", file=sys.stderr)

    os.environ["DOCQA_LIBRARY"] = library
    os.environ["DSH_MODEL"] = model
    os.environ["DOCQA_PROFILE"] = profile
    os.environ["DOCQA_INLINE_BUDGET"] = str(inline_budget)
    if dsh_bin:
        os.environ["DOCQA_DSH_BIN"] = dsh_bin
    if permission_mode:
        os.environ["DOCQA_PERMISSION_MODE"] = permission_mode
        if permission_mode == "danger-full-access":
            print(f"[docqa] 权限      {permission_mode}"
                  "（agent 不再受写围栏约束，也不再弹审批；这是 Windows 打包运行时下"
                  " shell/检索可用的唯一模式，见 README「Windows 沙箱 runner」）")
        else:
            print(f"[docqa] 权限      {permission_mode}")
    else:
        print("[docqa] 权限      跟随 dsh 默认（workspace-write）")

    # ------------------------------------------------------------------
    # 两层 --patch 覆写（都叠在 sdk profile 之上）：
    #   1) cordis.patch.yml     —— token 级流式转发（runtime 进程内的
    #      `agent/assistant-stream` 只有插件看得见，Python SDK 那条 JSON-RPC
    #      通道不转发它；插件把增量 POST 回本进程，再由 SSE 的 delta 事件推给浏览器）。
    #   2) genui.patch.yml      —— dsh-genui 的宿主半边：把 ```dsh-ui 围栏的组件
    #      词汇表注入系统提示词，并注册 validate_dsh_ui（校验/自动修复围栏 JSON）。
    #      浏览器半边由 docqa 自己的 vanilla 渲染器承担。
    # 两者挂不上都不会致命：最多是回答退回按步整段出现、模型不再主动输出围栏。
    # ------------------------------------------------------------------
    patch_files: list[Path] = []

    stream_patch = HERE / "docqa" / "plugin" / "cordis.patch.yml"
    if not args.no_stream and stream_patch.exists():
        os.environ["DOCQA_STREAM_HOOK"] = f"http://127.0.0.1:{args.port}/api/agent-stream"
        os.environ["DOCQA_STREAM_TOKEN"] = secrets.token_hex(16)
        patch_files.append(stream_patch)
        print(f"[docqa] 流式转发 已挂载 {stream_patch}")
    else:
        os.environ.pop("DOCQA_STREAM_HOOK", None)
        if not args.no_stream:
            print(f"[docqa] 流式转发 未找到 {stream_patch}，跳过（回答按步整段出现）", file=sys.stderr)

    genui_patch = HERE / "docqa" / "plugin" / "genui.patch.yml"
    if not args.no_genui and genui_patch.exists():
        patch_files.append(genui_patch)
        print(f"[docqa] GenUI     已挂载 {genui_patch}")
        # 渲染端的 mermaid / three.js 资产：直接引用插件安装目录里的那两个 IIFE，
        # 不拷贝（mermaid 3.4MB、three 704KB），由 /plugin-assets/* 提供。
        assets = Path(genui_assets) if genui_assets else DEFAULT_GENUI_ASSETS
        if assets.exists():
            os.environ["DOCQA_GENUI_ASSETS"] = str(assets)
            print(f"[docqa] GenUI 资产 {assets}")
        else:
            print(f"[docqa] GenUI 资产未找到（mermaid/scene3d 将不可用）：{assets}", file=sys.stderr)
    elif not args.no_genui:
        print(f"[docqa] GenUI     未找到 {genui_patch}，跳过（模型不会主动输出围栏）", file=sys.stderr)

    if patch_files:
        os.environ["DOCQA_PATCHES"] = os.pathsep.join(str(item) for item in patch_files)
    else:
        os.environ.pop("DOCQA_PATCHES", None)

    import uvicorn

    from docqa.server import create_app, default_engine

    engine = default_engine(library=library, model=model)
    app = create_app(engine)

    print(f"[docqa] 库目录   {engine.library.root}")
    print(f"[docqa] 会话     {engine.session_id}")
    print(f"[docqa] 模型     {engine.config.model} / profile={engine.config.profile}")
    print(f"[docqa] 接口     {engine.config.base_url or 'DeepSeek 官方默认'}")
    print(f"[docqa] 打开     http://{args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
