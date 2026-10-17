# -*- coding: utf-8 -*-
"""
transcoder.py — 调用 ffmpeg 把解密得到的 flac / ogg 转成 mp3。

只负责一件事：找到 ffmpeg，然后安全地调用它。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent

# Windows 下不要把 ffmpeg 的控制台窗口弹出来
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 优先使用随程序放置的 ffmpeg
BUNDLED_CANDIDATES: List[Path] = [
    BASE_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",
    BASE_DIR / "ffmpeg" / "ffmpeg.exe",
    BASE_DIR / "ffmpeg.exe",
    BASE_DIR / "bin" / "ffmpeg.exe",
    BASE_DIR / "tools" / "ffmpeg.exe",
]

# 再找系统里常见的 ffmpeg
SYSTEM_CANDIDATES: List[Path] = [
    Path(r"D:\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"),
    Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
    Path(r"D:\ffmpeg\bin\ffmpeg.exe"),
    Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
    Path(r"D:\Program Files\LosslessCut\resources\ffmpeg.exe"),
    Path(r"C:\Program Files\LosslessCut\resources\ffmpeg.exe"),
    Path(r"D:\Program Files\ShareX\ffmpeg.exe"),
    Path(r"C:\Program Files\ShareX\ffmpeg.exe"),
]

_found_ffmpeg: Optional[str] = None
_mp3_support: Optional[bool] = None

MP3_BITRATES = ["128k", "192k", "256k", "320k"]


def _is_file(path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def find_ffmpeg(preferred: Optional[str] = None, force: bool = False) -> Optional[str]:
    """定位 ffmpeg.exe。preferred 是用户手动指定的路径。"""
    global _found_ffmpeg

    if preferred and _is_file(preferred):
        _found_ffmpeg = str(preferred)
        return _found_ffmpeg

    if _found_ffmpeg and not force and _is_file(_found_ffmpeg):
        return _found_ffmpeg

    for cand in BUNDLED_CANDIDATES + SYSTEM_CANDIDATES:
        if _is_file(cand):
            _found_ffmpeg = str(cand)
            return _found_ffmpeg

    which = shutil.which("ffmpeg")
    if which:
        _found_ffmpeg = which
        return _found_ffmpeg

    _found_ffmpeg = None
    return None


def ffprobe_path(ffmpeg: Optional[str] = None) -> Optional[str]:
    """ffmpeg 同目录下的 ffprobe（可能没有，不强制）。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return None
    cand = Path(ffmpeg).with_name("ffprobe.exe")
    if cand.is_file():
        return str(cand)
    which = shutil.which("ffprobe")
    return which


def _run(args: List[str], timeout: int = 1800) -> Tuple[int, str, str]:
    """静默执行外部程序，返回 (返回码, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"命令超时（>{timeout}s）"
    except OSError as exc:
        return -1, "", f"无法启动程序：{exc}"

    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


def check_mp3_support(ffmpeg: Optional[str] = None) -> bool:
    """确认这个 ffmpeg 带 libmp3lame 编码器（精简版可能没有）。"""
    global _mp3_support
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False
    if _mp3_support is not None:
        return _mp3_support

    code, out, err = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=60)
    _mp3_support = "libmp3lame" in (out + err)
    return _mp3_support


def probe_audio(path, ffmpeg: Optional[str] = None) -> Optional[dict]:
    """用 ffprobe 读取音频信息；没有 ffprobe 时返回 None。"""
    probe = ffprobe_path(ffmpeg)
    if not probe:
        return None
    code, out, err = _run(
        [probe, "-v", "error", "-show_entries",
         "format=format_name,duration,bit_rate",
         "-show_entries", "stream=codec_name,channels,sample_rate",
         "-of", "default=nw=1", str(path)],
        timeout=120,
    )
    if code != 0:
        return None
    info = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()
    return info or None


def is_valid_audio(path, ffmpeg: Optional[str] = None) -> bool:
    """输出文件能否被解码器识别。"""
    info = probe_audio(path, ffmpeg)
    return bool(info.get("format_name"))


def to_mp3(
    src,
    dst,
    bitrate: str = "320k",
    ffmpeg: Optional[str] = None,
    keep_cover: bool = True,
) -> Tuple[bool, str]:
    """把 src 转码为 mp3 写到 dst。返回 (是否成功, 错误信息)。"""
    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return False, "没有找到 ffmpeg，无法转成 MP3。"

    if not check_mp3_support(ffmpeg):
        return False, "当前 ffmpeg 不支持 libmp3lame 编码器，无法生成 MP3。"

    src = Path(src)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)

    tmp = dst.with_name(dst.name + ".part")

    args = [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-v", "error",
        "-i", str(src),
        "-map", "0:a:0",
    ]
    if keep_cover:
        # 可选地带上内嵌封面（没有视频流也不会报错）
        args += ["-map", "0:v?", "-c:v", "copy"]

    args += [
        "-map_metadata", "0",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-id3v2_version", "3",
        "-f", "mp3",
        str(tmp),
    ]

    code, out, err = _run(args)
    if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, err or f"ffmpeg 返回码 {code}"

    os.replace(tmp, dst)
    return True, ""


if __name__ == "__main__":
    ff = find_ffmpeg()
    print("ffmpeg :", ff)
    print("probe  :", ffprobe_path(ff))
    print("mp3 支持:", check_mp3_support(ff))
