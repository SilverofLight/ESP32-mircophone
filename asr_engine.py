#!/usr/bin/env python3
"""用 Qwen3-ASR-GGUF 识别刚录下的音频。

本机是 AMD RX 9070：Encoder 走 ONNX CPU，Decoder 走 llama.cpp Vulkan。
首次使用前需要 third_party/Qwen3-ASR-GGUF、models/ 下的 0.6B 权重，
以及 inference/bin 里的 Vulkan 版 libllama。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "third_party" / "Qwen3-ASR-GGUF"
MODEL_DIR = ROOT / "models"
BIN_DIR = VENDOR_DIR / "qwen_asr_gguf" / "inference" / "bin"
PATCH_LLAMA = ROOT / "patches" / "llama.py"

REQUIRED_MODEL_FILES = (
    "qwen3_asr_encoder_frontend.int4.onnx",
    "qwen3_asr_encoder_backend.int4.onnx",
    "qwen3_asr_llm.q4_k.gguf",
)
REQUIRED_LIBS = ("libllama.so", "libggml.so", "libggml-base.so", "libggml-vulkan.so")

_engine = None
_lock = threading.Lock()
_loaded = False


def missing_resources() -> list[str]:
    missing: list[str] = []
    if not (VENDOR_DIR / "qwen_asr_gguf" / "inference" / "asr.py").exists():
        missing.append(str(VENDOR_DIR))
    for name in REQUIRED_MODEL_FILES:
        path = MODEL_DIR / name
        if not path.exists():
            missing.append(str(path))
    for name in REQUIRED_LIBS:
        path = BIN_DIR / name
        if not path.exists():
            missing.append(str(path))
    return missing


def _ensure_llama_abi() -> None:
    """llama.cpp b10859 的结构体与上游 Qwen 脚本不一致，需打补丁。"""
    target = VENDOR_DIR / "qwen_asr_gguf" / "inference" / "llama.py"
    if not target.exists() or not PATCH_LLAMA.exists():
        return
    text = target.read_text(encoding="utf-8")
    if "load_mtp" in text and "n_rs_seq" in text:
        return
    target.write_text(PATCH_LLAMA.read_text(encoding="utf-8"), encoding="utf-8")
    print("已应用 llama.cpp b10859 结构体补丁")


def _prepare_runtime() -> None:
    missing = missing_resources()
    if missing:
        joined = "\n  ".join(missing)
        raise FileNotFoundError(
            "缺少 Qwen3-ASR 运行文件:\n  "
            + joined
            + "\n请参考仓库说明下载模型与 llama.cpp Vulkan 库。"
        )

    _ensure_llama_abi()
    bin_dir = str(BIN_DIR)
    os.environ["LD_LIBRARY_PATH"] = bin_dir + os.pathsep + os.environ.get(
        "LD_LIBRARY_PATH", ""
    )
    vendor = str(VENDOR_DIR)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)


def load_engine(*, verbose: bool = True, n_ctx: int = 2048, use_gpu: bool = True):
    """加载识别引擎。模型常驻内存，只应调用一次。"""
    global _engine, _loaded
    with _lock:
        if _loaded:
            return _engine
        _prepare_runtime()
        from qwen_asr_gguf.inference.asr import QwenASREngine
        from qwen_asr_gguf.inference.schema import ASREngineConfig

        config = ASREngineConfig(
            model_dir=str(MODEL_DIR),
            encoder_frontend_fn="qwen3_asr_encoder_frontend.int4.onnx",
            encoder_backend_fn="qwen3_asr_encoder_backend.int4.onnx",
            llm_fn="qwen3_asr_llm.q4_k.gguf",
            onnx_provider="CPU",
            llm_use_gpu=use_gpu,
            n_ctx=n_ctx,
            chunk_size=40.0,
            memory_num=1,
            verbose=verbose,
            enable_aligner=False,
        )
        if verbose:
            print("正在加载 Qwen3-ASR（ONNX CPU + llama.cpp Vulkan）...")
        t0 = time.time()
        _engine = QwenASREngine(config=config)
        if verbose:
            print(f"Qwen3-ASR 就绪，耗时 {time.time() - t0:.2f} 秒")
        _loaded = True
        return _engine


def transcribe_file(
    audio_path: str | Path,
    *,
    language: str | None = "Chinese",
    context: str = "",
) -> str:
    """识别一个音频文件，返回文本。"""
    engine = load_engine()
    if engine is None:
        raise RuntimeError("ASR 引擎未加载")

    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(path)

    print(f"\n--- 开始识别: {path.name} ---")
    result = engine.transcribe(
        audio_file=str(path),
        language=language,
        context=context or None,
        start_second=0,
        duration=None,
    )
    text = (result.text or "").strip()
    try:
        from qwen_asr_gguf.inference.chinese_itn import chinese_to_num

        text = chinese_to_num(text).strip()
    except Exception:
        pass

    if not text:
        print("识别结果为空")
        return ""

    txt_path = path.with_suffix(".txt")
    txt_path.write_text(text + "\n", encoding="utf-8")
    print("\n========== 识别结果 ==========")
    print(text)
    print(f"==============================\n已写入 {txt_path}")
    return text


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="用 Qwen3-ASR 识别音频")
    parser.add_argument("audio", nargs="?", help="音频文件路径")
    parser.add_argument("--language", default="Chinese")
    parser.add_argument("--context", default="")
    parser.add_argument("--cpu-only", action="store_true", help="LLM 也走 CPU（调试用）")
    args = parser.parse_args()

    missing = missing_resources()
    if missing:
        print("缺少文件:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        return 1

    load_engine(use_gpu=not args.cpu_only)
    if not args.audio:
        return 0
    transcribe_file(args.audio, language=args.language or None, context=args.context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
