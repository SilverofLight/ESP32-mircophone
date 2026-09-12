#!/usr/bin/env python3
"""把 ESP32 推流注册成 PipeWire/Pulse 虚拟麦克风，并设为默认输入。

只创建 Audio/Source，往 FIFO 写 PCM，不经过任何播放流，避免窜到音箱。
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path

SOURCE_NAME = "esp32_mic"
SOURCE_DESC = "ESP32麦克风"
SAMPLE_RATE = 16000
FIFO_PATH = Path("/tmp/esp32_mic.fifo")


def _run(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=True,
        text=True,
    )


def _get_default_source() -> str | None:
    result = _run(["pactl", "get-default-source"])
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def _set_default_source(name: str) -> bool:
    return _run(["pactl", "set-default-source", name]).returncode == 0


def _source_module_id(name: str) -> str | None:
    result = _run(["pactl", "list", "sources"])
    if result.returncode != 0:
        return None
    current: str | None = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("Name: "):
            current = line.split(" ", 1)[1]
        elif line.startswith("Owner Module: ") and current == name:
            module_id = line.split(" ", 2)[2]
            if module_id and module_id != "n/a":
                return module_id
    return None


class VirtualMic:
    def __init__(self) -> None:
        self._active = False
        self._module_id: str | None = None
        self._prev_source: str | None = None
        self._fd: int | None = None
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=80)
        self._thread: threading.Thread | None = None

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        if self._active:
            return True
        if shutil.which("pactl") is None:
            print("未找到 pactl，无法注册虚拟麦克风")
            return False

        self._prev_source = _get_default_source()
        leftover = _source_module_id(SOURCE_NAME)
        if leftover:
            _run(["pactl", "unload-module", leftover])

        try:
            if FIFO_PATH.exists() or FIFO_PATH.is_fifo():
                FIFO_PATH.unlink()
        except OSError as exc:
            print(f"无法清理旧 FIFO: {exc}")
            return False

        result = _run(
            [
                "pactl",
                "load-module",
                "module-pipe-source",
                f"source_name={SOURCE_NAME}",
                f"file={FIFO_PATH}",
                "format=s16le",
                f"rate={SAMPLE_RATE}",
                "channels=1",
                f"source_properties=device.description={SOURCE_DESC}",
            ]
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            print(f"创建虚拟麦克风失败: {detail or 'pactl load-module 出错'}")
            return False
        self._module_id = result.stdout.strip() or None
        _run(["pactl", "suspend-source", SOURCE_NAME, "0"])

        if not _set_default_source(SOURCE_NAME):
            print("已创建虚拟麦克风，但设为默认输入失败")

        try:
            # O_RDWR 避免没人读时 open/write 阻塞；我们只写不读，音频不会被自己吸走。
            self._fd = os.open(FIFO_PATH, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            print(f"打开虚拟麦克风 FIFO 失败: {exc}")
            self._unload_module()
            if self._prev_source:
                _set_default_source(self._prev_source)
            return False

        self._thread = threading.Thread(target=self._writer, name="esp32-virtual-mic", daemon=True)
        self._thread.start()
        self._active = True
        current = _get_default_source()
        print(f"虚拟麦克风已就绪：{SOURCE_DESC}（{SOURCE_NAME}），当前默认输入 {current}")
        return True

    def feed(self, pcm: bytes) -> None:
        if not self._active or not pcm:
            return
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except queue.Full:
                pass

    def stop(self) -> None:
        if not self._active and self._fd is None and self._module_id is None:
            return
        self._active = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._close_fifo()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        current = _get_default_source()
        self._unload_module()
        try:
            if FIFO_PATH.exists() or FIFO_PATH.is_fifo():
                FIFO_PATH.unlink()
        except OSError:
            pass
        if self._prev_source and (current is None or current == SOURCE_NAME):
            _set_default_source(self._prev_source)
            print(f"已恢复默认输入：{self._prev_source}")
        self._prev_source = None

    def _close_fifo(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def _unload_module(self) -> None:
        module_id = self._module_id
        self._module_id = None
        if not module_id:
            return
        _run(["pactl", "unload-module", module_id])

    def _writer(self) -> None:
        while True:
            chunk = self._queue.get()
            if chunk is None:
                return
            fd = self._fd
            if fd is None:
                return
            view = memoryview(chunk)
            while view:
                try:
                    written = os.write(fd, view)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if written <= 0:
                    break
                view = view[written:]
