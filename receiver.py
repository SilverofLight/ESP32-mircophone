#!/usr/bin/env python3
"""连接 ESP32-S3 的 BLE 麦克风，实时接收 PCM 并在松开按钮后保存为 MP3。

ESP32-S3 只有 BLE，没有经典蓝牙。请先烧录 esp32_microphone.ino，
再在本机打开蓝牙适配器后运行本脚本。

示例:
  python receiver.py
  python receiver.py --name ESP32-MIC -o recordings
  python receiver.py --address AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice

DEVICE_NAME = "ESP32-MIC"
SERVICE_UUID = "e9ea0001-7dca-4e3d-9a9a-1c4f6b8e0001"
STATUS_CHAR_UUID = "e9ea0002-7dca-4e3d-9a9a-1c4f6b8e0001"
AUDIO_CHAR_UUID = "e9ea0003-7dca-4e3d-9a9a-1c4f6b8e0001"

EVENT_STOP = 0
EVENT_START = 1
STATUS_STRUCT = struct.Struct("<BIBBI")


class RecordingSession:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.pcm = bytearray()
        self.recording = False
        self.sample_rate = 16000
        self.bits = 16
        self.channels = 1
        self.expected_bytes = 0
        self.flush_at: float | None = None
        self.last_seq: int | None = None
        self.lost_packets = 0
        self.started_at = 0.0

    def reset(self) -> None:
        self.pcm.clear()
        self.recording = False
        self.expected_bytes = 0
        self.flush_at = None
        self.last_seq = None
        self.lost_packets = 0
        self.started_at = 0.0

    def handle_status(self, payload: bytes) -> None:
        if len(payload) < STATUS_STRUCT.size:
            print(f"忽略过短的状态包 ({len(payload)} 字节)")
            return

        event, sample_rate, bits, channels, pcm_bytes = STATUS_STRUCT.unpack_from(payload)
        self.sample_rate = int(sample_rate)
        self.bits = int(bits)
        self.channels = int(channels)

        if event == EVENT_START:
            self.pcm.clear()
            self.recording = True
            self.expected_bytes = 0
            self.flush_at = None
            self.last_seq = None
            self.lost_packets = 0
            self.started_at = time.monotonic()
            print(
                f"开始录音  {self.sample_rate} Hz / {self.bits} bit / {self.channels} ch"
            )
            return

        if event == EVENT_STOP:
            if not self.recording and not self.pcm:
                return
            self.recording = False
            self.expected_bytes = int(pcm_bytes)
            self.flush_at = time.monotonic() + 0.25
            print(f"停止录音  设备声明 {self.expected_bytes} 字节")
            return

        print(f"未知状态事件: {event}")

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
            self.pcm.extend(pcm)

    def ready_to_save(self) -> bool:
        if self.flush_at is None:
            return False
        if self.expected_bytes and len(self.pcm) >= self.expected_bytes:
            return True
        return time.monotonic() >= self.flush_at

    def save_if_ready(self) -> Path | None:
        if not self.ready_to_save():
            return None
        path = self.save()
        self.reset()
        return path

    def save(self) -> Path | None:
        if self.bits != 16:
            print(f"暂不支持 {self.bits} bit 音频")
            return None
        if not self.pcm:
            print("没有收到音频数据，跳过保存")
            return None

        bytes_per_sec = max(1, self.sample_rate * self.channels * (self.bits // 8))
        duration = len(self.pcm) / bytes_per_sec
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = unique_path(self.output_dir / f"recording_{stamp}.mp3")

        if self.lost_packets:
            print(f"警告: 约丢失 {self.lost_packets} 个 BLE 音频包")
        if self.expected_bytes and len(self.pcm) != self.expected_bytes:
            print(
                f"警告: 收到 {len(self.pcm)} 字节，设备声明 {self.expected_bytes} 字节"
            )

        try:
            pcm_to_mp3(bytes(self.pcm), path, self.sample_rate, self.channels)
        except Exception as exc:
            wav_path = path.with_suffix(".wav")
            print(f"MP3 编码失败 ({exc})，改存 WAV: {wav_path}")
            pcm_to_wav(bytes(self.pcm), wav_path, self.sample_rate, self.channels)
            print(f"已保存 {wav_path}  ({duration:.2f}s, {len(self.pcm)} 字节)")
            return wav_path

        print(f"已保存 {path}  ({duration:.2f}s, {len(self.pcm)} 字节)")
        return path


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 2
    while True:
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def pcm_to_wav(pcm: bytes, path: Path, sample_rate: int, channels: int) -> None:
    import wave

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def pcm_to_mp3(pcm: bytes, path: Path, sample_rate: int, channels: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("未找到 ffmpeg，请先安装")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        "-i",
        "pipe:0",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(path),
    ]
    result = subprocess.run(cmd, input=pcm, capture_output=True, check=False)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err or f"ffmpeg 退出码 {result.returncode}")


async def find_device(name: str, address: str | None, timeout: float) -> BLEDevice:
    if address:
        print(f"正在按地址扫描 {address} ...")
        device = await BleakScanner.find_device_by_address(address, timeout=timeout)
        if device is None:
            raise RuntimeError(f"未找到地址为 {address} 的设备")
        return device

    print(f"正在扫描 BLE 设备 {name} ...")
    device = await BleakScanner.find_device_by_name(name, timeout=timeout)
    if device is None:
        raise RuntimeError(
            f"未找到名为 {name} 的设备。请确认 ESP32 已上电并在广播。"
        )
    return device


async def run(args: argparse.Namespace) -> None:
    session = RecordingSession(Path(args.output_dir))
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
    disconnected = asyncio.Event()

    def on_disconnect(_: BleakClient) -> None:
        print("BLE 连接已断开")
        loop.call_soon_threadsafe(disconnected.set)

    def on_status(_: BleakGATTCharacteristic, data: bytearray) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("status", bytes(data)))

    def on_audio(_: BleakGATTCharacteristic, data: bytearray) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, ("audio", bytes(data)))

    device = await find_device(args.name, args.address, args.timeout)
    print(f"找到设备: {device.name or '未知'}  [{device.address}]")
    print("正在连接 ...")

    async with BleakClient(
        device, disconnected_callback=on_disconnect, timeout=args.timeout
    ) as client:
        try:
            print(f"已连接，MTU={client.mtu_size}（Linux 上此值可能始终显示 23）")
        except Exception:
            print("已连接")

        await client.start_notify(STATUS_CHAR_UUID, on_status)
        await client.start_notify(AUDIO_CHAR_UUID, on_audio)
        print("等待按下按钮录音，Ctrl+C 退出")

        try:
            while not disconnected.is_set():
                timeout = 0.05
                if session.flush_at is not None:
                    timeout = max(0.01, session.flush_at - time.monotonic())
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    session.save_if_ready()
                    continue

                if kind == "status":
                    session.handle_status(payload)
                elif kind == "audio":
                    session.handle_audio(payload)
                session.save_if_ready()
        except asyncio.CancelledError:
            raise
        finally:
            if session.pcm:
                session.flush_at = time.monotonic()
                session.save_if_ready()
            try:
                await client.stop_notify(AUDIO_CHAR_UUID)
                await client.stop_notify(STATUS_CHAR_UUID)
            except Exception:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ESP32-S3 INMP441 BLE 录音接收端")
    parser.add_argument("--name", default=DEVICE_NAME, help="BLE 广播名称")
    parser.add_argument("--address", help="直接按蓝牙地址连接")
    parser.add_argument("-o", "--output-dir", default="recordings", help="MP3 输出目录")
    parser.add_argument("--timeout", type=float, default=20.0, help="扫描/连接超时秒数")
    return parser.parse_args()


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("未找到 ffmpeg，保存 MP3 会失败。请先安装 ffmpeg。", file=sys.stderr)

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
