#!/usr/bin/env python3
"""把文本写入剪贴板并用 ydotool 向当前焦点模拟粘贴。

普通输入框用 Ctrl+V；当前焦点是终端时用 Ctrl+Shift+V。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

# linux/input-event-codes.h
KEY_LEFTCTRL = 29
KEY_LEFTSHIFT = 42
KEY_V = 47
KEY_BACKSPACE = 14
KEY_ENTER = 28

_backspace_held = False

TERMINAL_APP_IDS = {
    "kitty",
    "alacritty",
    "foot",
    "footclient",
    "wezterm",
    "org.wezfurlong.wezterm",
    "ghostty",
    "com.mitchellh.ghostty",
    "konsole",
    "org.kde.konsole",
    "gnome-terminal",
    "org.gnome.terminal",
    "org.gnome.console",
    "kgx",
    "xfce4-terminal",
    "org.xfce.terminal",
    "terminator",
    "tilix",
    "com.gexperts.tilix",
    "ptyxis",
    "org.gnome.ptyxis",
    "blackbox",
    "com.raggesilver.blackbox",
    "contour",
    "xterm",
    "uxterm",
    "urxvt",
    "rxvt",
    "st",
    "rio",
    "tabby",
    "hyper",
    "cool-retro-term",
    "io.elementary.terminal",
    "warp",
}


def _focused_app_id() -> str | None:
    niri = shutil.which("niri")
    if niri is None:
        return None
    try:
        proc = subprocess.run(
            [niri, "msg", "-j", "focused-window"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    app_id = data.get("app_id") if isinstance(data, dict) else None
    return str(app_id) if app_id else None


def focused_is_terminal() -> bool:
    app_id = _focused_app_id()
    if not app_id:
        return False
    return app_id.strip().lower() in TERMINAL_APP_IDS


def paste_text(text: str) -> bool:
    """复制到剪贴板并粘贴到当前键盘焦点。成功返回 True。"""
    payload = (text or "").strip()
    if not payload:
        return False

    wl_copy = shutil.which("wl-copy")
    ydotool = shutil.which("ydotool")
    if wl_copy is None:
        print("未找到 wl-copy，跳过自动粘贴", file=sys.stderr)
        return False
    if ydotool is None:
        print("未找到 ydotool，跳过自动粘贴", file=sys.stderr)
        return False

    try:
        subprocess.run(
            [wl_copy, "--"],
            input=payload.encode("utf-8"),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"wl-copy 失败，跳过自动粘贴: {exc}", file=sys.stderr)
        return False

    terminal = focused_is_terminal()
    if terminal:
        keys = [
            f"{KEY_LEFTCTRL}:1",
            f"{KEY_LEFTSHIFT}:1",
            f"{KEY_V}:1",
            f"{KEY_V}:0",
            f"{KEY_LEFTSHIFT}:0",
            f"{KEY_LEFTCTRL}:0",
        ]
        shortcut = "Ctrl+Shift+V"
    else:
        keys = [
            f"{KEY_LEFTCTRL}:1",
            f"{KEY_V}:1",
            f"{KEY_V}:0",
            f"{KEY_LEFTCTRL}:0",
        ]
        shortcut = "Ctrl+V"

    time.sleep(0.08)
    try:
        subprocess.run([ydotool, "key", *keys], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ydotool 粘贴失败: {exc}", file=sys.stderr)
        return False

    print(f"已用 {shortcut} 粘贴润色结果到当前焦点")
    return True


def _ydotool_key(*keys: str) -> bool:
    ydotool = shutil.which("ydotool")
    if ydotool is None:
        print("未找到 ydotool，跳过按键", file=sys.stderr)
        return False
    try:
        subprocess.run([ydotool, "key", *keys], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ydotool 按键失败: {exc}", file=sys.stderr)
        return False
    return True


def tap_backspace() -> bool:
    if _ydotool_key(f"{KEY_BACKSPACE}:1", f"{KEY_BACKSPACE}:0"):
        print("已单击退格")
        return True
    return False


def hold_backspace(start: bool) -> bool:
    global _backspace_held
    if start:
        if _backspace_held:
            return True
        if _ydotool_key(f"{KEY_BACKSPACE}:1"):
            _backspace_held = True
            print("已按住退格")
            return True
        return False
    if not _backspace_held:
        return True
    if _ydotool_key(f"{KEY_BACKSPACE}:0"):
        _backspace_held = False
        print("已松开退格")
        return True
    return False


def tap_enter() -> bool:
    if _ydotool_key(f"{KEY_ENTER}:1", f"{KEY_ENTER}:0"):
        print("已单击回车")
        return True
    return False
