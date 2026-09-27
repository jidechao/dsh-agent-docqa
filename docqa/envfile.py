"""项目根目录 `.env` 的加载器（web / CLI / Streamlit 三个入口共用）。

为什么要宿主自己读：dsh 运行时的凭据插件确实支持 `.env` 分层，但它只认
**自己的调用目录**（本项目中是 `<library>/workspace`）和 **`$DSH_HOME`**
（`<library>/home`）这两处，项目根目录不在其中。所以「项目自己的 .env」
必须由宿主在读取 `os.environ` 之前加载。

优先级（左高右低）：

1. 真实环境变量（shell 里已 export 的）；
2. 项目 `.env`（本模块；`--env-file` 可改路径，`--no-env-file` 可关）；
3. `~/.dsh/.credentials.yaml`（托管凭据，只补 ``DEEPSEEK_API_KEY``）。

加载器**永不覆盖**已存在的变量，所以 `.env` 只能「补配置」，不会破坏它被
启动时的 shell 环境（PATH、虚拟环境、代理等一律以真实环境为准）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: 合法的 POSIX 风格变量名；其它键名一律忽略，避免把 `foo bar=` 写进环境。
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str) -> dict[str, str]:
    """最小 dotenv 解析器（python-dotenv 不可用时的回退）。

    支持：空行、`#` 注释、`export ` 前缀、`KEY=VALUE`、单/双引号包裹、
    双引号内的 ``\\n`` / ``\\t`` / ``\\"`` / ``\\\\`` 转义，以及未加引号值末尾
    由空白引入的行内注释。
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, rest = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        if not _NAME_RE.match(name):
            continue
        value = rest.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (
                    value.replace("\\n", "\n")
                    .replace("\\t", "\t")
                    .replace('\\"', '"')
                    .replace("\\\\", "\\")
                )
        else:
            # 未加引号：只把「空白 + #」当注释起点，值里的 # 原样保留。
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        values[name] = value
    return values


def read_env_file(path: Path) -> dict[str, str]:
    """读取一个 dotenv 文件；不存在或读不动就返回空字典。"""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:  # 优先用 python-dotenv：对引号/转义/多行值的处理更完整
        from dotenv import dotenv_values  # type: ignore[import-not-found]

        loaded = dotenv_values(path, encoding="utf-8")
        return {str(k): str(v) for k, v in loaded.items() if v is not None}
    except Exception:
        return parse_env_text(text)


def load_env_file(path: Path, *, override: bool = False) -> list[str]:
    """把 ``path`` 里的键值写进 ``os.environ``，返回**实际生效**的键名。

    ``override=False``（默认）时已存在的变量保持不动——真实环境永远赢。
    返回值只含键名，调用方可以安全地打印出来（绝不打印值）。
    """
    applied: list[str] = []
    for name, value in read_env_file(Path(path)).items():
        if not _NAME_RE.match(name):
            continue
        if not override and os.environ.get(name):
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


__all__ = ["load_env_file", "parse_env_text", "read_env_file"]
