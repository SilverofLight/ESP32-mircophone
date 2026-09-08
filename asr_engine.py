#!/usr/bin/env python3
"""用 Qwen3-ASR-GGUF 识别刚录下的音频。

本机是 AMD RX 9070：Encoder 走 ONNX CPU，Decoder 走 llama.cpp Vulkan。
首次使用前需要 third_party/Qwen3-ASR-GGUF、models/ 下的 0.6B 权重，
以及 inference/bin 里的 Vulkan 版 libllama。

录音过程只缓存 PCM；松开按钮后整段识别，再用 Qwen3-0.6B 润色口癖。
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
VENDOR_DIR = ROOT / "third_party" / "Qwen3-ASR-GGUF"
MODEL_DIR = ROOT / "models"
BIN_DIR = VENDOR_DIR / "qwen_asr_gguf" / "inference" / "bin"
PATCH_LLAMA = ROOT / "patches" / "llama.py"
POLISH_MODEL_FN = "Qwen3-0.6B-Q8_0.gguf"

REQUIRED_MODEL_FILES = (
    "qwen3_asr_encoder_frontend.int4.onnx",
    "qwen3_asr_encoder_backend.int4.onnx",
    "qwen3_asr_llm.q4_k.gguf",
)
REQUIRED_LIBS = ("libllama.so", "libggml.so", "libggml-base.so", "libggml-vulkan.so")

POLISH_PROMPT = (
    "<|im_start|>system\n"
    "你负责清理语音识别文本。只删除语气词、口头禅和明显口误重复，"
    "不改变原意，不补充内容，不解释，不使用 Markdown。<|im_end|>\n"
    "<|im_start|>user\n"
    "请清理下面的识别结果，删除「呃、额、嗯、啊、唔、那个、就是」这类语气词。"
    "只输出清理后的文本。\n\n"
    "{text}<|im_end|>\n"
    "<|im_start|>assistant\n"
    "<think>\n\n</think>\n\n"
)

_engine = None
_polisher = None
_lock = threading.Lock()
_loaded = False
_polisher_loaded = False


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


def polish_model_path() -> Path:
    return MODEL_DIR / POLISH_MODEL_FN


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
            chunk_size=2.0,
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


def load_polisher(*, verbose: bool = True, use_gpu: bool = True):
    """加载 Qwen3-0.6B 润色模型。与 ASR Decoder 共用 llama.cpp，但用独立上下文。"""
    global _polisher, _polisher_loaded
    with _lock:
        if _polisher_loaded:
            return _polisher
        path = polish_model_path()
        if not path.exists():
            if verbose:
                print(f"未找到润色模型 {path.name}，将只输出 ASR 原文")
            _polisher_loaded = True
            _polisher = None
            return None
        _prepare_runtime()
        if verbose:
            print("正在加载 Qwen3-0.6B 润色模型（llama.cpp Vulkan）...")
        t0 = time.time()
        try:
            _polisher = TextPolisher(path, use_gpu=use_gpu)
        except Exception as exc:
            print(f"润色模型加载失败，将只输出 ASR: {exc}", file=sys.stderr)
            _polisher = None
            _polisher_loaded = True
            return None
        if verbose:
            print(f"润色模型就绪，耗时 {time.time() - t0:.2f} 秒")
        _polisher_loaded = True
        return _polisher


def _normalize_language(language: str | None) -> str | None:
    if language is None:
        return None
    text = str(language).strip()
    if not text or text.lower() in {"auto", "none"}:
        return None
    from qwen_asr_gguf.inference.utils import normalize_language_name, validate_language

    name = normalize_language_name(text)
    validate_language(name)
    return name


def _apply_itn(text: str) -> str:
    try:
        from qwen_asr_gguf.inference.chinese_itn import chinese_to_num

        return chinese_to_num(text).strip()
    except Exception:
        return text.strip()


def transcribe_audio(engine, audio: np.ndarray, *, language: str | None, context: str | None) -> str:
    """对整段 16 kHz float32 PCM 做一次 ASR，录音过程中不调用。"""
    if audio.size == 0:
        return ""
    sr = 16000
    duration = audio.size / sr
    chunk_size_sec = 40.0 if duration > 40.0 else max(duration, 0.4)
    samples_per_chunk = max(1, int(round(chunk_size_sec * sr)))
    num_chunks = int(np.ceil(audio.size / samples_per_chunk))
    memory: deque = deque(maxlen=1)
    parts: list[str] = []
    for i in range(num_chunks):
        start = i * samples_per_chunk
        stop = min((i + 1) * samples_per_chunk, audio.size)
        chunk = audio[start:stop]
        if chunk.size < samples_per_chunk:
            chunk = np.pad(chunk, (0, samples_per_chunk - chunk.size))
        audio_feature, _ = engine.encoder.encode(chunk)
        prefix_text = "".join(item[1] for item in memory)
        if memory:
            combined = np.concatenate(
                [item[0] for item in memory] + [audio_feature], axis=0
            )
        else:
            combined = audio_feature
        full_embd = engine._build_prompt_embd(
            combined, prefix_text, context, language
        )
        result = engine._safe_decode(
            full_embd,
            prefix_text,
            rollback_num=5,
            is_last_chunk=(i == num_chunks - 1),
            temperature=0.4,
            streaming=False,
        )
        piece = result.text or ""
        memory.append((audio_feature, piece))
        parts.append(piece)
    return "".join(parts).strip()


def _clean_polish_output(raw: str, original: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"</?think>", "", text).strip()
    text = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text).strip()
    if len(text) >= 2 and (
        (text[0], text[-1]) in {("「", "」"), ("“", "”"), ('"', '"'), ("'", "'")}
    ):
        text = text[1:-1].strip()
    text = re.sub(
        r"^(清理后的文本|清理后|润色结果|润色|输出)\s*[：:]\s*",
        "",
        text,
    ).strip()
    if not text:
        return original
    if len(text) > max(40, int(len(original) * 2.5)):
        return original
    return text


class TextPolisher:
    """用 Qwen3-0.6B 去掉转写里的语气词，不改原意。"""

    def __init__(self, model_path: Path, *, use_gpu: bool = True, n_ctx: int = 2048) -> None:
        from qwen_asr_gguf.inference import llama

        self.llama = llama
        self.model = llama.LlamaModel(str(model_path), use_gpu=use_gpu)
        if not getattr(self.model, "ptr", None):
            raise RuntimeError(f"润色模型加载失败: {model_path}")
        self.ctx = llama.LlamaContext(
            self.model,
            n_ctx=n_ctx,
            n_batch=n_ctx,
            n_ubatch=min(512, n_ctx),
        )
        self.id_im_end = self.model.token_to_id("<|im_end|>")
        self.eos = self.model.eos_token

    def polish(self, text: str) -> str:
        original = (text or "").strip()
        if not original:
            return ""
        prompt = POLISH_PROMPT.format(text=original)
        tokens = self.model.tokenize(prompt, add_special=False, parse_special=True)
        if not tokens:
            return original
        self.ctx.clear_kv_cache()
        for token in tokens:
            if self.ctx.decode_token(token) != 0:
                return original
        sampler = self.llama.LlamaSampler(temperature=0.2, top_k=20, top_p=0.8, seed=1)
        out_tokens: list[int] = []
        try:
            last = sampler.sample(self.ctx)
            stop = {self.eos, self.id_im_end, -1}
            for _ in range(256):
                if last in stop:
                    break
                out_tokens.append(last)
                if self.ctx.decode_token(last) != 0:
                    break
                last = sampler.sample(self.ctx)
        finally:
            sampler.free()
        raw = self.model.detokenize(out_tokens).strip()
        return _clean_polish_output(raw, original)


class LiveTranscriber:
    """录音期间只缓存 PCM，松开后整段识别并润色。"""

    def __init__(
        self,
        engine,
        *,
        sample_rate: int = 16000,
        language: str | None = "Chinese",
        context: str = "",
        polisher: TextPolisher | None = None,
    ) -> None:
        self.engine = engine
        self.polisher = polisher
        self.input_rate = sample_rate
        self.min_last_samples = max(1, int(0.2 * 16000))
        self.language = _normalize_language(language)
        self.context = context or None
        self.pending = np.zeros(0, dtype=np.float32)

    def feed_pcm16(self, data: bytes) -> None:
        if not data:
            return
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) * (1.0 / 32768.0)
        if self.input_rate != 16000:
            samples = _resample_linear(samples, self.input_rate, 16000)
        if self.pending.size:
            self.pending = np.concatenate([self.pending, samples])
        else:
            self.pending = samples

    def finish(self) -> str:
        audio = self.pending
        self.pending = np.zeros(0, dtype=np.float32)
        if audio.size < self.min_last_samples:
            print("\n识别结果为空")
            return ""
        print("正在识别 ...")
        text = _apply_itn(
            transcribe_audio(
                self.engine,
                audio,
                language=self.language,
                context=self.context,
            )
        )
        polished = text
        if self.polisher is not None and text:
            print("正在润色 ...")
            try:
                polished = self.polisher.polish(text)
            except Exception as exc:
                print(f"润色失败，沿用 ASR 原文: {exc}", file=sys.stderr)
                polished = text
        if text:
            print("\n========== 本段识别 ==========")
            print(f"ASR : {text}")
            print(f"润色: {polished}")
            print("==============================")
        else:
            print("\n识别结果为空")
        return text


def _resample_linear(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio
    duration = audio.size / src_rate
    dst_len = max(1, int(round(duration * dst_rate)))
    src_x = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, dst_len, endpoint=False)
    return np.interp(dst_x, src_x, audio).astype(np.float32)


class StreamingRecognizer:
    """在独立线程里跑 ASR 与润色，避免堵住 BLE 收包。"""

    def __init__(
        self,
        *,
        language: str | None = "Chinese",
        context: str = "",
        use_gpu: bool = True,
        polish: bool = True,
    ) -> None:
        self.language = language
        self.context = context
        self.use_gpu = use_gpu
        self.polish = polish
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="asr-worker", daemon=True)
        self._thread.start()

    def start_utterance(self, sample_rate: int = 16000) -> None:
        self._queue.put(("start", sample_rate))

    def feed(self, pcm16: bytes) -> None:
        if pcm16:
            self._queue.put(("pcm", pcm16))

    def end_utterance(self, timeout: float = 60.0) -> str:
        done = threading.Event()
        box: dict[str, str] = {"text": ""}
        self._queue.put(("end", (done, box)))
        if not done.wait(timeout):
            print("语音识别超时", file=sys.stderr)
            return box["text"]
        return box["text"]

    def close(self) -> None:
        self._queue.put(("close", None))

    def _loop(self) -> None:
        engine = load_engine(use_gpu=self.use_gpu, verbose=True)
        polisher = load_polisher(use_gpu=self.use_gpu, verbose=True) if self.polish else None
        live: LiveTranscriber | None = None
        while True:
            cmd, payload = self._queue.get()
            try:
                if cmd == "start":
                    live = LiveTranscriber(
                        engine,
                        sample_rate=int(payload or 16000),
                        language=self.language,
                        context=self.context,
                        polisher=polisher,
                    )
                elif cmd == "pcm" and live is not None:
                    live.feed_pcm16(payload)
                elif cmd == "end":
                    done, box = payload
                    if live is not None:
                        box["text"] = live.finish()
                        live = None
                    done.set()
                elif cmd == "close":
                    if live is not None:
                        live.finish()
                    return
            except Exception as exc:
                print(f"语音识别失败: {exc}", file=sys.stderr)
                if cmd == "end":
                    payload[0].set()


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
    parser.add_argument("--polish", help="直接用 0.6B 模型润色一段文本并退出")
    args = parser.parse_args()

    missing = missing_resources()
    if missing:
        print("缺少文件:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        return 1

    if args.polish:
        polisher = load_polisher(use_gpu=not args.cpu_only)
        if polisher is None:
            print("润色模型不可用", file=sys.stderr)
            return 1
        print(polisher.polish(args.polish))
        return 0

    load_engine(use_gpu=not args.cpu_only)
    if not args.audio:
        return 0
    transcribe_file(args.audio, language=args.language or None, context=args.context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
