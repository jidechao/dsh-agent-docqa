"""Regression test for the permission-mode plumbing — offline, no runtime.

Run with the project venv::

    .venv\\Scripts\\python.exe tests\\test_permission_mode.py

Why: on Windows the packaged SDK runtime cannot host the Windows ACL sandbox
runner, so every `pwsh` / `grep` / `glob` call dies with
`error: --profile <name> is required`. The supported way out is DSH's documented
deployment override `DSH_PERMISSION_MODE=danger-full-access`, which removes the
need for that runner. This test pins the wiring: the knob reaches the runtime
child's environment, the default stays untouched, and bad values fail loudly.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docqa.engine import PERMISSION_MODES, DocQAEngine, EngineConfig, resolve_permission_mode  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not condition:
        FAILURES.append(label)


class FakeHarness:
    """Captures the constructor kwargs the engine would hand to the SDK."""

    instances: list[dict] = []

    def __init__(self, **kwargs) -> None:
        FakeHarness.instances.append(kwargs)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def run(self, *args, **kwargs):  # pragma: no cover - not reached
        raise AssertionError("this test must not run a turn")


def install_fake_harness():
    """Put a fake `deepseek_harness` module in place (the engine imports it lazily)."""
    import types

    module = types.ModuleType("deepseek_harness")
    module.DeepSeekHarness = FakeHarness
    sys.modules["deepseek_harness"] = module
    return module


def main() -> int:
    install_fake_harness()

    # ---------------------------------------------------------------- validation
    check("all three DSH presets are known", PERMISSION_MODES == ("read-only", "workspace-write", "danger-full-access"),
          str(PERMISSION_MODES))
    check("blank values mean 'leave dsh alone'", resolve_permission_mode("", "   ", None) is None)
    check("the first non-empty value wins", resolve_permission_mode(None, "read-only", "danger-full-access") == "read-only")
    check("DSH's own spelling is accepted", resolve_permission_mode("danger-full-access") == "danger-full-access")
    try:
        resolve_permission_mode("yolo")
        check("unknown preset is rejected", False, "no error raised")
    except ValueError as error:
        check("unknown preset is rejected", "yolo" in str(error), str(error))

    with tempfile.TemporaryDirectory(prefix="docqa-permission-test-") as scratch:
        root = Path(scratch) / "library"

        def harness_kwargs(**config) -> dict:
            FakeHarness.instances.clear()
            engine = DocQAEngine(EngineConfig(library_root=root, **config))
            engine._harness_or_start()
            return FakeHarness.instances[-1]

        # ------------------------------------------------------------- default off
        kwargs = harness_kwargs()
        check("default leaves DSH_PERMISSION_MODE unset", "DSH_PERMISSION_MODE" not in kwargs["env"],
              str(kwargs["env"]))
        check("runtime still gets its home and workspace", bool(kwargs["dsh_home"]) and bool(kwargs["cwd"]))

        # -------------------------------------------------------------- opt-in on
        kwargs = harness_kwargs(permission_mode="danger-full-access")
        check("danger-full-access reaches the runtime child",
              kwargs["env"].get("DSH_PERMISSION_MODE") == "danger-full-access", str(kwargs["env"]))
        kwargs = harness_kwargs(permission_mode="read-only")
        check("read-only reaches the runtime child", kwargs["env"].get("DSH_PERMISSION_MODE") == "read-only")

        # ------------------------------------------- credentials must not be broken
        kwargs = harness_kwargs(api_key="sk-test", base_url="https://example.invalid/v1", permission_mode="danger-full-access")
        check("api key still passed", kwargs["env"].get("DEEPSEEK_API_KEY") == "sk-test")
        check("base url still passed", kwargs["env"].get("DEEPSEEK_BASE_URL") == "https://example.invalid/v1")

        # ------------------------------------------- server.default_engine reads env
        from docqa.server import default_engine

        saved = {name: os.environ.get(name) for name in ("DOCQA_PERMISSION_MODE", "DSH_PERMISSION_MODE", "DOCQA_LIBRARY")}
        try:
            os.environ.pop("DOCQA_PERMISSION_MODE", None)
            os.environ.pop("DSH_PERMISSION_MODE", None)
            os.environ["DOCQA_LIBRARY"] = str(root)
            check("default_engine leaves the mode alone when unset",
                  default_engine().config.permission_mode is None)
            os.environ["DOCQA_PERMISSION_MODE"] = "danger-full-access"
            check("default_engine honours DOCQA_PERMISSION_MODE",
                  default_engine().config.permission_mode == "danger-full-access")
            os.environ.pop("DOCQA_PERMISSION_MODE", None)
            os.environ["DSH_PERMISSION_MODE"] = "danger-full-access"
            check("default_engine honours DSH_PERMISSION_MODE",
                  default_engine().config.permission_mode == "danger-full-access")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
