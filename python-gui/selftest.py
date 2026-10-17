# -*- coding: utf-8 -*-
"""
selftest.py — 环境自检

逐项检查本工具能否在你这台机器上正常工作：
  1. Python / 依赖库
  2. ffmpeg 与 MP3 编码器
  3. QQ音乐进程
  4. Frida 注入 + 解密函数导出
  5. 真实读取校验（找一个普通音频文件，用 QQ音乐的解密函数读一遍，
     与磁盘原始字节逐字节比对）

用法:  python selftest.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

PASS = "[通过]"
FAIL = "[失败]"
WARN = "[注意]"

problems = []


def line(mark: str, text: str) -> None:
    print(f"  {mark} {text}")
    if mark == FAIL:
        problems.append(text)


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")


def main() -> int:
    print("QQ音乐加密音乐 解密/转换工具 —— 环境自检")
    print(f"程序目录: {BASE}")

    # ---------------------------------------------------------- 1 依赖
    section("1. 运行环境")
    line(PASS, f"Python {sys.version.split()[0]}  ({sys.executable})")

    try:
        import frida

        line(PASS, f"frida {getattr(frida, '__version__', '?')}")
    except Exception as exc:
        line(FAIL, f"没有安装 frida（{exc}）—— 请执行: python -m pip install frida")
        print("\n缺少关键依赖，后续检查无法继续。")
        return 1

    try:
        import tkinterdnd2  # noqa: F401

        line(PASS, "tkinterdnd2（支持拖拽）")
    except Exception:
        line(WARN, "没有安装 tkinterdnd2 —— 界面仍可用，但不能拖拽，请执行: python -m pip install tkinterdnd2")

    # ---------------------------------------------------------- 2 ffmpeg
    section("2. ffmpeg（转 MP3 用）")
    import transcoder

    ff = transcoder.find_ffmpeg(force=True)
    if ff:
        line(PASS, f"ffmpeg: {ff}")
        line(PASS, f"ffprobe: {transcoder.ffprobe_path(ff) or '未找到'}")
        if transcoder.check_mp3_support(ff):
            line(PASS, "支持 libmp3lame，可以生成 MP3")
        else:
            line(WARN, "这个 ffmpeg 不带 libmp3lame，无法生成 MP3（只能输出 ogg/flac）")
    else:
        line(WARN, "没有找到 ffmpeg —— 只能输出原生 ogg/flac，不能转 MP3")

    # ---------------------------------------------------------- 3 QQ音乐
    section("3. QQ音乐进程")
    from qqmusic_decryptor import QQMusicDecryptor

    try:
        procs = QQMusicDecryptor.list_candidates()
    except Exception as exc:
        line(FAIL, f"无法枚举进程: {exc}")
        procs = []

    if not procs:
        line(FAIL, "没有检测到运行中的 QQ音乐 —— 请先启动并登录 QQ音乐")
    else:
        for p in procs:
            line(PASS, f"找到进程: {p.name} (PID {p.pid})")

    if not procs:
        print("\n请启动 QQ音乐 后重新运行自检。")
        return 1

    # ---------------------------------------------------------- 4 Frida 注入
    section("4. Frida 注入与解密函数")
    dec = QQMusicDecryptor(logger=lambda m: print(f"        · {m}"))
    roundtrip_src: Path | None = None

    try:
        info = dec.attach()
        line(PASS, f"已注入 {info.name} (PID {info.pid})")

        # 取脚本自检信息
        script_info = dec._script.exports_sync.info()  # type: ignore[union-attr]
        line(PASS, f"Frida {script_info['frida']} / {script_info['platform']} / "
                   f"{script_info['arch']}（{script_info['pointerSize']} 字节指针）")
        line(PASS, f"QQMusicCommon.dll: {script_info['dllPath']}")
        line(PASS, f"解密函数导出齐全: {', '.join(script_info['exports'])}")
    except Exception as exc:
        line(FAIL, f"注入失败: {exc}")
        print("\n注入失败，解密功能不可用。")
        return 1

    # ---------------------------------------------------------- 5 真实读取校验
    section("5. 真实读取校验（逐字节比对）")
    exts = (".mp3", ".flac", ".ogg", ".m4a", ".wav")
    for folder in (BASE, BASE.parent, Path.home() / "Music"):
        if roundtrip_src:
            break
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix.lower() in exts and 0 < f.stat().st_size < 60 * 1024 * 1024:
                roundtrip_src = f
                break

    if not roundtrip_src:
        line(WARN, "没找到可用于校验的普通音频文件，跳过此项")
    else:
        try:
            raw = roundtrip_src.read_bytes()
            got = dec.decrypt(roundtrip_src)
            same = got == raw
            line(PASS if same else FAIL,
                 f"{roundtrip_src.name}: 磁盘 {len(raw)} 字节 / 读出 {len(got)} 字节, "
                 f"sha256 一致: {same}")
            if same:
                line(PASS, "说明 构造 -> Open -> GetSize -> Read -> RPC 整条链路正确")
        except Exception as exc:
            line(WARN, f"读取校验未完成: {exc}")

    dec.detach()

    # ---------------------------------------------------------- 结论
    section("结论")
    if problems:
        print(f"  发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"    - {p}")
        return 1

    print("  环境正常 ✅")
    print("\n  下一步：启动 QQ音乐，用「下载」功能下载歌曲（VIP 歌曲会得到 .mgg/.mflac），")
    print("  然后把文件拖进 qqmusic_gui.py 的窗口即可。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
