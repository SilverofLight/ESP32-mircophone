#!/usr/bin/env python3
"""把 receiver 的连接状态写到运行时文件，供其他程序读取。

状态：disconnected（未连接）、connected（已连接）、mic（麦克风模式）。
receiver 退出后删除该文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_DISCONNECTED = "disconnected"
STATE_CONNECTED = "connected"
STATE_MIC = "mic"

LABELS = {
    STATE_DISCONNECTED: "未连接",
    STATE_CONNECTED: "已连接",
    STATE_MIC: "麦克风模式",
}


def status_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) if runtime else Path("/tmp")
    return root / "esp32-mic" / "status"


class LinkStatus:
    def __init__(self) -> None:
        self.connected = False
        self.live_mic = False
        self.address: str | None = None
        self.name: str | None = None
        self.path = status_path()

    def set_device(self, address: str | None, name: str | None) -> None:
        if address:
            self.address = address
        if name:
            self.name = name
        self.flush()

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.live_mic = False
        self.flush()

    def set_live_mic(self, on: bool) -> None:
        self.live_mic = bool(on) and self.connected
        self.flush()

    def state(self) -> str:
        if not self.connected:
            return STATE_DISCONNECTED
        if self.live_mic:
            return STATE_MIC
        return STATE_CONNECTED

    def flush(self) -> None:
        state = self.state()
        payload = {
            "state": state,
            "label": LABELS[state],
            "pid": os.getpid(),
            "address": self.address,
            "name": self.name,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def close(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass
        try:
            self.path.with_name(self.path.name + ".tmp").unlink()
        except OSError:
            pass
