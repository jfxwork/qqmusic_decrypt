# -*- coding: utf-8 -*-
"""
qqmusic_decryptor.py
====================
QQ音乐加密音乐解密核心模块。

原理
----
不需要自己实现 QMC 加密算法，也不需要抓取密钥：QQ音乐本身要播放这些加密文件，
所以 QQMusicCommon.dll 里必然有“解密读取”的代码。逆向成果（来自
luyikk/qqmusic_decrypt）发现该类为 C++ 的 EncAndDesMediaFile：

    构造 -> Open(路径, true, false) -> GetSize() -> Read(buf, size, 0) -> 析构

借用 QQ音乐自己的这段代码即可解密，好处是：
  * 任何新版本加密格式（mflac / mgg / mgg1 / mflac0 / mggl ...）自动支持；
  * 不需要维护密钥表，也不会随客户端升级失效。

约束
----
* 必须安装并运行 QQ音乐（解密代码在它自己的进程里）。
* 通过 Frida 注入 JS，调用上述导出函数。

本模块把该流程封装成可复用的 Python 接口。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import frida

# 加密扩展名 -> 解密后原生扩展名（当内容嗅探无法判定时作为兜底）
EXT_MAP = {
    ".mgg": ".ogg",
    ".mgg1": ".ogg",
    ".mggl": ".ogg",
    ".mflac": ".flac",
    ".mflac0": ".flac",
    ".mflac1": ".flac",
    ".mmp4": ".m4a",
}

# 内容嗅探：文件头 -> 扩展名
MAGIC_MAP = (
    (b"fLaC", ".flac"),
    (b"OggS", ".ogg"),
    (b"ID3", ".mp3"),
    (b"\xff\xfb", ".mp3"),
    (b"\xff\xf3", ".mp3"),
    (b"\xff\xf2", ".mp3"),
)

HOOK_JS_PATH = Path(__file__).with_name("hook_qq_music.js")


class QQMusicNotRunning(RuntimeError):
    """QQ音乐进程没有找到。"""


class DecryptError(RuntimeError):
    """解密过程失败。"""


def sniff_extension(data: bytes, fallback: str = ".bin") -> str:
    """根据解密后的文件内容判断真实格式。"""
    for magic, ext in MAGIC_MAP:
        if data.startswith(magic):
            return ext
    # MP4/M4A: ....ftyp
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".m4a"
    # WAV
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return ".wav"
    return fallback


def native_extension(src: str | os.PathLike) -> str:
    """按源文件扩展名推断解密后的原生扩展名。"""
    ext = Path(src).suffix.lower()
    return EXT_MAP.get(ext, ext or ".bin")


def is_encrypted_music(path: str | os.PathLike) -> bool:
    """是否是本工具能处理的加密音乐文件。"""
    p = Path(path)
    return p.is_file() and p.suffix.lower() in EXT_MAP


@dataclass
class ProcessInfo:
    pid: int
    name: str


class QQMusicDecryptor:
    """管理到 QQ音乐的 Frida 会话，并对外提供 decrypt()。

    线程安全：内部加锁，可在工作线程中安全调用。
    """

    def __init__(self, logger: Optional[Callable[[str], None]] = None) -> None:
        self._log = logger or (lambda msg: None)
        self._lock = threading.RLock()
        self._session: Optional[frida.core.Session] = None
        self._script: Optional[frida.core.Script] = None
        self._pid: Optional[int] = None
        self._name: Optional[str] = None
        self._frida_version = getattr(frida, "__version__", "?")

    # ------------------------------------------------------------------ 进程
    @staticmethod
    def list_candidates() -> List[ProcessInfo]:
        """列出所有看起来是 QQ音乐的进程。"""
        try:
            device = frida.get_local_device()
            procs = device.enumerate_processes()
        except Exception as exc:  # pragma: no cover
            raise DecryptError(f"无法枚举进程（Frida 初始化失败）：{exc}") from exc

        out: List[ProcessInfo] = []
        for p in procs:
            low = p.name.lower()
            if "qqmusic" in low:
                out.append(ProcessInfo(pid=p.pid, name=p.name))
        return out

    @property
    def frida_version(self) -> str:
        return self._frida_version

    @property
    def attached(self) -> bool:
        return self._script is not None

    @property
    def target(self) -> Optional[ProcessInfo]:
        if self._pid is None:
            return None
        return ProcessInfo(pid=self._pid, name=self._name or "QQMusic")

    # ------------------------------------------------------------------ 连接
    def attach(self, pid: Optional[int] = None) -> ProcessInfo:
        """连接到 QQ音乐并注入解密脚本。重复调用是安全的。"""
        with self._lock:
            if self._script is not None:
                return self.target  # type: ignore[return-value]

            candidates = self.list_candidates()
            if not candidates:
                raise QQMusicNotRunning(
                    "没有找到运行中的 QQ音乐。请先启动 QQ音乐并登录，再重试。"
                )

            if pid is not None:
                chosen = next((c for c in candidates if c.pid == pid), None)
                if chosen is None:
                    raise QQMusicNotRunning(f"进程 {pid} 已经不存在了，请重新连接。")
            else:
                # 优先选主进程（QQMusic.exe），避免注入到辅助进程
                chosen = next(
                    (c for c in candidates if c.name.lower() == "qqmusic.exe"),
                    candidates[0],
                )

            if not HOOK_JS_PATH.is_file():
                raise DecryptError(f"缺少解密脚本：{HOOK_JS_PATH}")

            js_source = HOOK_JS_PATH.read_text(encoding="utf-8")

            try:
                device = frida.get_local_device()
                session = device.attach(chosen.pid)
                script = session.create_script(js_source)
                script.on("message", self._on_message)
                script.load()
                info = script.exports_sync.info()
            except frida.ProcessNotFoundError as exc:
                raise QQMusicNotRunning(
                    "QQ音乐进程已退出或不可访问，请重启 QQ音乐后重试。"
                ) from exc
            except Exception as exc:
                raise DecryptError(f"注入 QQ音乐失败：{exc}") from exc

            self._session = session
            self._script = script
            self._pid = chosen.pid
            self._name = chosen.name

            self._log(
                f"已连接 QQ音乐 ({chosen.name}, PID {chosen.pid})；"
                f"Frida {info.get('frida')} / {info.get('arch')} "
                f"({info.get('pointerSize')} 字节指针)"
            )
            return chosen

    def detach(self) -> None:
        with self._lock:
            try:
                if self._script is not None:
                    self._script.unload()
            except Exception:
                pass
            try:
                if self._session is not None:
                    self._session.detach()
            except Exception:
                pass
            self._script = None
            self._session = None
            self._pid = None
            self._name = None

    def _on_message(self, message, data) -> None:
        if message.get("type") == "error":
            self._log(f"[JS 错误] {message.get('description')}")

    # ------------------------------------------------------------------ 解密
    def decrypt(self, src: str | os.PathLike) -> bytes:
        """解密单个文件，返回明文数据。"""
        src = str(Path(src))
        if not os.path.isfile(src):
            raise DecryptError(f"文件不存在：{src}")

        with self._lock:
            if self._script is None:
                self.attach()
            try:
                data = self._script.exports_sync.decrypt(src)
            except Exception as exc:
                # 会话可能因 QQ音乐重启而失效，重连一次再试
                self._log(f"解密调用失败（{exc}），尝试重新连接…")
                self.detach()
                self.attach()
                try:
                    data = self._script.exports_sync.decrypt(src)
                except Exception as exc2:
                    raise DecryptError(str(exc2)) from exc2

        if data is None:
            raise DecryptError("解密返回了空数据。")
        raw = bytes(data)
        if not raw:
            raise DecryptError("解密结果长度为 0。")
        return raw

    def decrypt_to_file(
        self,
        src: str | os.PathLike,
        out_dir: str | os.PathLike,
        overwrite: bool = False,
        progress: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """解密并写入 out_dir，返回实际写出的文件路径。"""
        src_p = Path(src)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        fallback = native_extension(src_p)
        data = self.decrypt(src_p)
        ext = sniff_extension(data, fallback)
        dst = out_dir / (src_p.stem + ext)

        if dst.exists() and not overwrite:
            if progress:
                progress(f"已存在，跳过：{dst.name}")
            return dst

        tmp = dst.with_suffix(dst.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        return dst
