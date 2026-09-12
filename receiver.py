#!/usr/bin/env python3
"""连接 ESP32-S3 的 BLE 麦克风，按住录音，松开后识别。

ESP32-S3 只有 BLE。按下按钮开始推流，松开结束；PCM 不落盘。
松开后加载 GPU 模型，识别整段并用 Qwen3-0.6B 润色口癖，然后卸掉显存。
润色结果默认经 wl-copy + ydotool 粘贴到当前键盘焦点。

示例:
  python receiver.py
  python receiver.py --name ESP32-MIC
  python receiver.py --address AA:BB:CC:DD:EE:FF
  python receiver.py --keep-models
  python receiver.py --no-paste
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from pathlib import Path

import asr_engine

DEVICE_NAME = "ESP32-MIC"
SERVICE_UUID = "e9ea0001-7dca-4e3d-9a9a-1c4f6b8e0001"
STATUS_CHAR_UUID = "e9ea0002-7dca-4e3d-9a9a-1c4f6b8e0001"
AUDIO_CHAR_UUID = "e9ea0003-7dca-4e3d-9a9a-1c4f6b8e0001"

EVENT_STOP = 0
EVENT_START = 1
EVENT_BUTTON = 2
BUTTON_ACTION_CLICK = 1
BUTTON_ACTION_HOLD_START = 2
BUTTON_ACTION_HOLD_END = 3
STATUS_STRUCT = struct.Struct("<BIBBI")
ADDRESS_CACHE = Path(__file__).resolve().parent / ".ble_last_address"


class RecordingSession:
    def __init__(self, recognizer: asr_engine.StreamingRecognizer | None) -> None:
        self.recognizer = recognizer
        self.recording = False
        self.sample_rate = 16000
        self.bits = 16
        self.channels = 1
        self.expected_bytes = 0
        self.received_bytes = 0
        self.flush_at: float | None = None
        self.last_seq: int | None = None
        self.lost_packets = 0
        self.started_at = 0.0

    def reset_state(self) -> None:
        self.recording = False
        self.expected_bytes = 0
        self.received_bytes = 0
        self.flush_at = None
        self.last_seq = None
        self.lost_packets = 0
        self.started_at = 0.0

    def handle_status(self, payload: bytes) -> None:
        if not payload:
            return
        event = payload[0]
        if event == EVENT_BUTTON:
            self.handle_button(payload)
            return
        if len(payload) < STATUS_STRUCT.size:
            print(f"忽略过短的状态包 ({len(payload)} 字节)")
            return

        event, sample_rate, bits, channels, pcm_bytes = STATUS_STRUCT.unpack_from(payload)
        self.sample_rate = int(sample_rate)
        self.bits = int(bits)
        self.channels = int(channels)

        if event == EVENT_START:
            self.recording = True
            self.expected_bytes = 0
            self.received_bytes = 0
            self.flush_at = None
            self.last_seq = None
            self.lost_packets = 0
            self.started_at = time.monotonic()
            print(
                f"开始录音  {self.sample_rate} Hz / {self.bits} bit / {self.channels} ch"
            )
            if self.recognizer is not None:
                self.recognizer.start_utterance(self.sample_rate)
            return

        if event == EVENT_STOP:
            if not self.recording and self.flush_at is None:
                return
            self.recording = False
            self.expected_bytes = int(pcm_bytes)
            self.flush_at = time.monotonic() + 0.25
            print(f"停止录音  设备声明 {self.expected_bytes} 字节")
            return

        print(f"未知状态事件: {event}")

    def handle_button(self, payload: bytes) -> None:
        if len(payload) < 3:
            return
        button_id = payload[1]
        action = payload[2]
        if button_id != 2:
            print(f"按钮{button_id} 动作{action}")
            return
        from paste_input import hold_backspace, tap_backspace

        if action == BUTTON_ACTION_CLICK:
            tap_backspace()
        elif action == BUTTON_ACTION_HOLD_START:
            hold_backspace(True)
        elif action == BUTTON_ACTION_HOLD_END:
            hold_backspace(False)

    def handle_audio(self, payload: bytes) -> None:
        if len(payload) < 4:
            return

        seq = payload[0] | (payload[1] << 8)
        pcm = payload[2:]
        if len(pcm) % 2:
            pcm = pcm[:-1]
        if not pcm:
            return

        if self.last_seq is not None:
            expected = (self.last_seq + 1) & 0xFFFF
            if seq != expected:
                gap = (seq - expected) & 0xFFFF
                self.lost_packets += gap
        self.last_seq = seq

        if self.recording or self.flush_at is not None:
            self.received_bytes += len(pcm)
            if self.recognizer is not None:
                self.recognizer.feed(pcm)

    def ready_to_finish(self) -> bool:
        if self.flush_at is None:
            return False
        if self.expected_bytes and self.received_bytes >= self.expected_bytes:
            return True
        return time.monotonic() >= self.flush_at


async def finish_utterance(session: RecordingSession) -> None:
    if session.lost_packets:
        print(f"警告: 约丢失 {session.lost_packets} 个 BLE 音频包")
    if session.expected_bytes and session.received_bytes != session.expected_bytes:
        print(
            f"警告: 收到 {session.received_bytes} 字节，设备声明 {session.expected_bytes} 字节"
        )
    duration = 0.0
    if session.sample_rate and session.bits and session.channels:
        bytes_per_sec = session.sample_rate * session.channels * (session.bits // 8)
        if bytes_per_sec:
            duration = session.received_bytes / bytes_per_sec
    print(f"本段音频 {duration:.2f}s")
    if session.recognizer is not None:
        await asyncio.get_running_loop().run_in_executor(
            None, session.recognizer.end_utterance
        )
    session.reset_state()


def _read_cached_address() -> str | None:
    try:
        text = ADDRESS_CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _write_cached_address(address: str) -> None:
    try:
        ADDRESS_CACHE.write_text(address.strip() + "\n", encoding="utf-8")
    except OSError:
        pass


async def find_device(name: str, address: str | None, timeout: float) -> BLEDevice:
    if address:
        print(f"正在按地址扫描 {address} ...")
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise RuntimeError(f"未找到地址为 {address} 的设备")
        return device

    cached = _read_cached_address()
    if cached:
        print(f"正在连接上次设备 {cached} ...")
        device = await BleakScanner.find_device_by_address(cached, timeout=min(3.0, timeout))
        if device is not None:
            return device
        print("上次地址未找到，改为按名称扫描")

    print(f"正在扫描 BLE 设备 {name} ...")
    device = await BleakScanner.find_device_by_name(name, timeout=timeout)
    if device is None:
        raise RuntimeError(
            f"未找到名为 {name} 的设备。请确认 ESP32 已上电并在广播。"
        )
    return device


async def run(args: argparse.Namespace) -> None:
    asr_ok = False
    asr_kwargs: dict = {}
    if not args.no_asr:
        missing = asr_engine.missing_resources()
        if missing:
            print("未找到 Qwen3-ASR 模型或 llama.cpp 库，将只接收音频：")
            for item in missing:
                print(f"  {item}")
        else:
            asr_ok = True
            language = None if args.language.lower() in {"auto", "none", ""} else args.language
            asr_kwargs = {
                "language": language,
                "context": args.context,
                "use_gpu": not args.asr_cpu,
                "polish": not args.no_polish,
                "keep_models": args.keep_models,
                "paste": not args.no_paste,
            }

    session = RecordingSession(None)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
    disconnected = asyncio.Event()
    recognizer = None

    def on_disconnect(_: BleakClient) -> None:
        print("BLE 连接已断开")
        from paste_input import hold_backspace

        hold_backspace(False)
        loop.call_soon_threadsafe(disconnected.set)

    def on_status(_: BleakGATTCharacteristic, data: bytearray) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("status", bytes(data)))

    def on_audio(_: BleakGATTCharacteristic, data: bytearray) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("audio", bytes(data)))

    device = await find_device(args.name, args.address, args.timeout)
    _write_cached_address(device.address)
    print(f"找到设备: {device.name or '未知'}  [{device.address}]")
    print("正在连接 ...")

    async with BleakClient(
        device, disconnected_callback=on_disconnect, timeout=args.timeout
    ) as client:
        try:
            print(f"已连接，MTU={client.mtu_size}（Linux 上此值可能始终显示 23）")
        except Exception:
            print("已连接")

        if asr_ok:
            recognizer = asr_engine.StreamingRecognizer(**asr_kwargs)
            session.recognizer = recognizer

        await client.start_notify(STATUS_CHAR_UUID, on_status)
        await client.start_notify(AUDIO_CHAR_UUID, on_audio)
        hint = "松开后加载模型并识别"
        if recognizer is not None and not args.no_paste:
            hint += "，润色结果会粘贴到当前焦点"
        print(f"等待按下按钮说话，{hint}；按钮2 短按退格、长按按住退格，Ctrl+C 退出")

        try:
            while not disconnected.is_set():
                timeout = 0.05
                if session.flush_at is not None:
                    timeout = max(0.01, session.flush_at - time.monotonic())
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    if session.ready_to_finish():
                        await finish_utterance(session)
                    continue

                if kind == "status":
                    session.handle_status(payload)
                elif kind == "audio":
                    session.handle_audio(payload)
                if session.ready_to_finish():
                    await finish_utterance(session)
        except asyncio.CancelledError:
            raise
        finally:
            if session.recording or session.flush_at is not None:
                session.flush_at = time.monotonic()
                await finish_utterance(session)
            if recognizer is not None:
                recognizer.close()
            try:
                await client.stop_notify(AUDIO_CHAR_UUID)
                await client.stop_notify(STATUS_CHAR_UUID)
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ESP32-S3 INMP441 BLE 语音识别")
    parser.add_argument("--name", default=DEVICE_NAME, help="BLE 广播名称")
    parser.add_argument("--address", help="直接按蓝牙地址连接")
    parser.add_argument("--timeout", type=float, default=20.0, help="扫描/连接超时秒数")
    parser.add_argument("--no-asr", action="store_true", help="只收音频，不做语音识别")
    parser.add_argument("--no-polish", action="store_true", help="只输出 ASR，不调用 0.6B 润色")
    parser.add_argument(
        "--keep-models",
        action="store_true",
        help="识别后不卸载 GGUF，连续说话更快，但会一直占显存",
    )
    parser.add_argument(
        "--no-paste",
        action="store_true",
        help="只打印结果，不把润色文本粘贴到当前焦点",
    )
    parser.add_argument("--asr-cpu", action="store_true", help="ASR 的 LLM 走 CPU，不用 Vulkan")
    parser.add_argument("--language", default="Chinese", help="识别语言，auto 表示自动检测")
    parser.add_argument("--context", default="", help="ASR 上下文提示，可提高专有名词准确率")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已退出")
        return 0
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
