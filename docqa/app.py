"""Streamlit chat surface: upload documents, then ask questions about them."""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from .engine import DocQAEngine, EngineConfig, resolve_permission_mode
from .envfile import load_env_file

# 项目根目录的 .env（与 run_web.py / CLI 共用同一份配置；不覆盖已存在的环境变量）
load_env_file(Path(os.environ.get("DOCQA_ENV_FILE") or Path(__file__).resolve().parent.parent / ".env"))

st.set_page_config(page_title="个人文档问答助手", page_icon="📚", layout="wide")


@st.cache_resource(show_spinner=False)
def get_engine(library: str, provider: str, model: str, profile: str) -> DocQAEngine:
    return DocQAEngine(EngineConfig(
        library_root=Path(library),
        provider=provider,
        model=model,
        profile=profile,
        api_key=os.environ.get("DEEPSEEK_API_KEY"),
        base_url=os.environ.get("DEEPSEEK_BASE_URL"),
        permission_mode=resolve_permission_mode(
            os.environ.get("DOCQA_PERMISSION_MODE"), os.environ.get("DSH_PERMISSION_MODE")
        ),
    ))


def main() -> None:
    st.title("📚 个人文档问答助手")
    st.caption("上传 1 个或多个文档，然后针对文档提问。文件在本机转换，对话由 DeepSeek Harness agent 完成。")

    with st.sidebar:
        st.header("设置")
        library = st.text_input("资料库目录", value=os.environ.get("DOCQA_LIBRARY", ".docqa"))
        provider = st.text_input("Provider", value="deepseek-official")
        model = st.text_input("模型", value=os.environ.get("DSH_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "deepseek-flash")
        profile = st.text_input("Profile", value="sdk", help="sdk = 完整工具集；sdk-minimal 只有 shell")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            st.warning("未检测到 DEEPSEEK_API_KEY；若 SDK home 里没有托管凭据，模型调用会失败。")

        engine = get_engine(library, provider, model, profile)
        st.divider()
        st.caption(f"session：`{engine.session_id}`")
        if st.button("开始新对话", use_container_width=True):
            st.session_state.messages = []
            st.success(f"新 session：{engine.new_session()}")
        if st.button("清空文档", use_container_width=True):
            from .ingest import reset_library

            reset_library(engine.library)
            st.session_state.messages = []
            st.rerun()

    from .ingest import engine_availability

    availability = engine_availability()
    uploads = st.file_uploader(
        "上传文档（Office 全家桶 / PDF / Markdown / CSV / HTML / EPUB / 代码…）",
        accept_multiple_files=True,
    )
    if not availability.get("libreoffice"):
        st.caption("ℹ️ 未检测到 LibreOffice：`.doc`/`.ppt`/`.xls`/`.odt` 等旧格式无法转换。")
    if not availability.get("tesseract"):
        st.caption("ℹ️ 未检测到 tesseract：扫描版 PDF 只会被标记，不做 OCR。")
    if uploads:
        with st.status("正在转换文档…", expanded=True) as status:
            for upload in uploads:
                payload = upload.getvalue()
                entry = engine.add_document(payload, upload.name)
                target = entry.markdown_path or "（未转换）"
                st.write(f"`{entry.source_name}` → {target} · {entry.chars:,} 字符 {entry.note}")
            status.update(label=f"已入库 {len(engine.library.docs)} 个文档", state="complete")

    if engine.library.docs:
        with st.expander(f"资料库（{len(engine.library.docs)} 个文档，{engine.library.total_chars():,} 字符）"):
            for entry in engine.library.docs:
                st.markdown(f"- `{entry.source_name}` → `{entry.markdown_path or '—'}` {entry.note}")

    st.session_state.setdefault("messages", [])
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    question = st.chat_input("针对文档提问…")
    if not question:
        return
    if not engine.library.docs:
        st.warning("请先上传至少一个文档。")
        return

    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        trace = st.expander("执行轨迹", expanded=True)
        answer_box = st.empty()
        answer = ""
        for progress in engine.ask_stream(question):
            if progress.kind == "tool":
                trace.write(f"🔧 {progress.text}")
            elif progress.kind == "step":
                trace.write(f"▸ {progress.text}")
            elif progress.kind == "text" and progress.text.strip():
                answer = progress.text
                answer_box.markdown(answer)
            elif progress.kind == "done":
                answer = progress.text or answer
                answer_box.markdown(answer or "_（模型没有返回文本）_")
                trace.write(f"✅ finish_reason = {progress.data.get('finish_reason')}")
            elif progress.kind == "error":
                answer = f"**执行失败**：{progress.text}"
                answer_box.error(answer)
                trace.write(f"❌ {progress.text}")
            elif progress.kind == "note":
                trace.write(f"· {progress.text}")

    st.session_state.messages.append({"role": "assistant", "content": answer or "_（无输出）_"})


if __name__ == "__main__":
    main()
