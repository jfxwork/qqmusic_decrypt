# -*- coding: utf-8 -*-
"""
qqmusic_gui.py — QQ音乐加密音乐 解密 / 转换工具（图形界面）

把 QQ音乐下载的 .mgg / .mflac / .mgg1 / .mflac0 等加密文件拖进来，
自动解密成 ogg / flac，并可再转成 mp3。

依赖：frida（调用 QQ音乐自带的解密代码）、ffmpeg（转 mp3）、tkinterdnd2（拖拽）
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import transcoder
from qqmusic_decryptor import (
    EXT_MAP,
    DecryptError,
    QQMusicDecryptor,
    QQMusicNotRunning,
    native_extension,
    sniff_extension,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except Exception:  # pragma: no cover
    HAS_DND = False

APP_TITLE = "QQ音乐加密音乐 · 解密 / 转换"
CONFIG_PATH = BASE_DIR / "config.json"

FORMAT_NATIVE = "native"
FORMAT_MP3 = "mp3"
FORMAT_BOTH = "both"


# --------------------------------------------------------------------------- 数据
@dataclass
class Job:
    src: Path
    status: str = "等待"
    outputs: List[Path] = field(default_factory=list)
    error: str = ""
    iid: str = ""


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


# --------------------------------------------------------------------------- 主界面
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.jobs: List[Job] = []
        self.by_path: Dict[str, Job] = {}
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.decryptor = QQMusicDecryptor(logger=lambda m: self.post("log", m))
        self.env_ok = False

        self.var_format = tk.StringVar(value=FORMAT_BOTH)
        self.var_bitrate = tk.StringVar(value="320k")
        self.var_outmode = tk.StringVar(value="source")  # source | custom
        self.var_outdir = tk.StringVar(value=str(BASE_DIR / "output"))
        self.var_skip = tk.BooleanVar(value=True)
        self.var_delmid = tk.BooleanVar(value=False)
        self.var_ffmpeg = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="正在检测环境…")
        self.var_progress = tk.StringVar(value="就绪")

        self._build_ui()
        self._load_config()

        self.root.after(100, self._poll_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self.check_env, daemon=True).start()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        r = self.root
        r.title(APP_TITLE)
        r.geometry("980x740")
        r.minsize(860, 620)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # ---- 顶部：标题 + 连接状态
        top = ttk.Frame(r, padding=(12, 10, 12, 4))
        top.pack(fill="x")

        ttk.Label(top, text=APP_TITLE, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")

        btns = ttk.Frame(top)
        btns.pack(side="right")
        ttk.Button(btns, text="环境自检", width=10, command=self.check_env_clicked).pack(side="right", padx=(6, 0))
        ttk.Button(btns, text="重新连接", width=10, command=self.reconnect).pack(side="right")

        self.lbl_conn = ttk.Label(top, textvariable=self.var_status, foreground="#b45309")
        self.lbl_conn.pack(side="right", padx=12)

        # ---- 设置区
        box = ttk.LabelFrame(r, text="输出设置", padding=(12, 8, 12, 10))
        box.pack(fill="x", padx=12, pady=(6, 4))

        row1 = ttk.Frame(box)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="输出格式：", width=10).pack(side="left")
        ttk.Radiobutton(row1, text="原生格式（mgg→ogg，mflac→flac）",
                        variable=self.var_format, value=FORMAT_NATIVE).pack(side="left")
        ttk.Radiobutton(row1, text="MP3", variable=self.var_format,
                        value=FORMAT_MP3).pack(side="left", padx=10)
        ttk.Radiobutton(row1, text="原生 + MP3", variable=self.var_format,
                        value=FORMAT_BOTH).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="MP3 码率：", width=10).pack(side="left")
        ttk.Combobox(row2, textvariable=self.var_bitrate, width=8, state="readonly",
                     values=transcoder.MP3_BITRATES).pack(side="left")
        ttk.Label(row2, text="    ffmpeg：").pack(side="left")
        self.lbl_ffmpeg = ttk.Label(row2, text="检测中…", foreground="#555")
        self.lbl_ffmpeg.pack(side="left")
        ttk.Button(row2, text="指定…", width=7, command=self.choose_ffmpeg).pack(side="left", padx=6)

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=3)
        ttk.Label(row3, text="输出位置：", width=10).pack(side="left")
        ttk.Radiobutton(row3, text="源文件目录下的 output 文件夹",
                        variable=self.var_outmode, value="source").pack(side="left")
        ttk.Radiobutton(row3, text="指定目录", variable=self.var_outmode,
                        value="custom").pack(side="left", padx=(10, 4))
        ttk.Entry(row3, textvariable=self.var_outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(row3, text="浏览…", width=7, command=self.choose_outdir).pack(side="left", padx=6)

        row4 = ttk.Frame(box)
        row4.pack(fill="x", pady=3)
        ttk.Label(row4, text="", width=10).pack(side="left")
        ttk.Checkbutton(row4, text="跳过已存在的文件", variable=self.var_skip).pack(side="left")
        ttk.Checkbutton(row4, text="生成 MP3 后不保留原生文件（仅 MP3 模式生效）",
                        variable=self.var_delmid).pack(side="left", padx=16)

        # ---- 队列区
        mid = ttk.LabelFrame(r, text="待处理文件（可直接把文件或文件夹拖进窗口）", padding=(12, 8, 12, 10))
        mid.pack(fill="both", expand=True, padx=12, pady=4)

        # 按钮栏先占住底部，否则下面的 Treeview 用了 expand 会把它挤没
        bar = ttk.Frame(mid)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_add_file = ttk.Button(bar, text="添加文件", width=10, command=self.pick_files)
        self.btn_add_file.pack(side="left")
        self.btn_add_dir = ttk.Button(bar, text="添加文件夹", width=11, command=self.pick_folder)
        self.btn_add_dir.pack(side="left", padx=6)
        self.btn_remove = ttk.Button(bar, text="移除选中", width=10, command=self.remove_selected)
        self.btn_remove.pack(side="left")
        self.btn_clear = ttk.Button(bar, text="清空列表", width=10, command=self.clear_all)
        self.btn_clear.pack(side="left", padx=6)
        self._edit_buttons = [self.btn_add_file, self.btn_add_dir, self.btn_remove, self.btn_clear]

        treewrap = ttk.Frame(mid)
        treewrap.pack(side="top", fill="both", expand=True)

        cols = ("name", "size", "status", "output")
        self.tree = ttk.Treeview(treewrap, columns=cols, show="headings",
                                 height=9, selectmode="extended")
        self.tree.heading("name", text="文件名")
        self.tree.heading("size", text="大小")
        self.tree.heading("status", text="状态")
        self.tree.heading("output", text="输出")
        self.tree.column("name", width=330, anchor="w")
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("output", width=380, anchor="w")

        vsb = ttk.Scrollbar(treewrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("ok", foreground="#15803d")
        self.tree.tag_configure("err", foreground="#b91c1c")
        self.tree.tag_configure("run", foreground="#1d4ed8")
        self.tree.tag_configure("skip", foreground="#6b7280")

        # ---- 进度 + 操作
        act = ttk.Frame(r, padding=(12, 4, 12, 4))
        act.pack(fill="x")
        self.pb = ttk.Progressbar(act, mode="determinate", maximum=100)
        self.pb.pack(fill="x", pady=(4, 4))
        line = ttk.Frame(act)
        line.pack(fill="x")
        self.btn_start = ttk.Button(line, text="开始转换", width=12, command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(line, text="停止", width=8, command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(line, text="打开输出目录", width=12, command=self.open_output_dir).pack(side="left", padx=6)
        ttk.Label(line, textvariable=self.var_progress).pack(side="right")

        # ---- 日志
        logbox = ttk.LabelFrame(r, text="日志", padding=(8, 6, 8, 8))
        logbox.pack(fill="both", expand=False, padx=12, pady=(4, 12))
        self.log = tk.Text(logbox, height=8, wrap="word", state="disabled",
                           background="#111827", foreground="#d1d5db",
                           insertbackground="#d1d5db", relief="flat",
                           font=("Consolas", 9))
        lsb = ttk.Scrollbar(logbox, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

        # ---- 拖拽
        if HAS_DND:
            for w in (r, self.tree, mid):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self.on_drop)
                except Exception:
                    pass
        else:
            self.log_line("提示：未安装 tkinterdnd2，拖拽功能不可用，请用「添加文件 / 添加文件夹」按钮。")

    # ------------------------------------------------------------------ 队列通信
    def post(self, kind: str, *payload) -> None:
        self.ui_queue.put((kind, payload))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                handler = getattr(self, f"_ui_{kind}", None)
                if handler:
                    handler(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _ui_log(self, msg: str) -> None:
        self.log_line(msg)

    def _ui_conn(self, text: str, ok: bool) -> None:
        self.var_status.set(text)
        self.lbl_conn.configure(foreground="#15803d" if ok else "#b45309")

    def _ui_ffmpeg(self, text: str) -> None:
        self.lbl_ffmpeg.configure(text=text)

    def _ui_status(self, iid: str, status: str, tag: str = "") -> None:
        job = next((j for j in self.jobs if j.iid == iid), None)
        if job:
            job.status = status
        if self.tree.exists(iid):
            self.tree.set(iid, "status", status)
            self.tree.item(iid, tags=(tag,) if tag else ())

    def _ui_output(self, iid: str, text: str) -> None:
        if self.tree.exists(iid):
            self.tree.set(iid, "output", text)

    def _ui_progress(self, done: int, total: int, text: str) -> None:
        self.pb.configure(maximum=max(total, 1), value=done)
        self.var_progress.set(text)

    def _ui_done(self) -> None:
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        for b in self._edit_buttons:
            b.configure(state="normal")
        self.worker = None

    # ------------------------------------------------------------------ 日志
    def log_line(self, msg: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------ 环境
    def check_env_clicked(self) -> None:
        self.var_status.set("正在检测环境…")
        threading.Thread(target=self.check_env, daemon=True).start()

    def check_env(self) -> None:
        # ffmpeg
        ff = transcoder.find_ffmpeg(self.var_ffmpeg.get() or None, force=True)
        if ff:
            ok_mp3 = transcoder.check_mp3_support(ff)
            self.post("ffmpeg", f"{'支持 MP3' if ok_mp3 else '不支持 libmp3lame'} · {ff}")
            self.post("log", f"ffmpeg：{ff}（{'可生成 MP3' if ok_mp3 else '缺少 libmp3lame'}）")
        else:
            self.post("ffmpeg", "未找到 ffmpeg（无法转 MP3）")
            self.post("log", "警告：未找到 ffmpeg，只能输出原生 ogg/flac，不能转 MP3。")

        # QQ音乐
        try:
            procs = QQMusicDecryptor.list_candidates()
        except Exception as exc:
            self.post("conn", f"进程检测失败：{exc}", False)
            return
        if not procs:
            self.env_ok = False
            self.post("conn", "未检测到 QQ音乐（请先启动）", False)
            self.post("log", "未检测到运行中的 QQ音乐，请先启动并登录 QQ音乐。")
            return

        try:
            info = self.decryptor.attach()
            self.env_ok = True
            self.post("conn", f"已连接 QQ音乐 (PID {info.pid})", True)
            self.post("log", f"已连接 QQ音乐：{info.name} (PID {info.pid})，"
                             f"Frida {self.decryptor.frida_version}")
        except Exception as exc:
            self.env_ok = False
            self.post("conn", "QQ音乐连接失败", False)
            self.post("log", f"连接 QQ音乐失败：{exc}")

    def reconnect(self) -> None:
        self.decryptor.detach()
        self.check_env_clicked()

    def choose_ffmpeg(self) -> None:
        p = filedialog.askopenfilename(title="选择 ffmpeg.exe",
                                       filetypes=[("ffmpeg", "ffmpeg.exe"), ("可执行文件", "*.exe")])
        if p:
            self.var_ffmpeg.set(p)
            self.check_env_clicked()
            self._save_config()

    def choose_outdir(self) -> None:
        p = filedialog.askdirectory(title="选择输出目录")
        if p:
            self.var_outdir.set(p)
            self.var_outmode.set("custom")
            self._save_config()

    # ------------------------------------------------------------------ 添加文件
    def on_drop(self, event) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self.add_paths(paths)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(EXT_MAP))
        paths = filedialog.askopenfilenames(
            title="选择加密音乐文件",
            filetypes=[("QQ音乐加密文件", exts), ("全部文件", "*.*")],
        )
        if paths:
            self.add_paths(paths)

    def pick_folder(self) -> None:
        p = filedialog.askdirectory(title="选择包含加密音乐的文件夹")
        if p:
            self.add_paths([p])

    def add_paths(self, paths) -> None:
        found: List[Path] = []
        for raw in paths:
            raw = str(raw).strip().strip('"')
            if not raw:
                continue
            p = Path(raw)
            if p.is_dir():
                for ext in EXT_MAP:
                    found.extend(sorted(p.rglob(f"*{ext}")))
                    found.extend(sorted(p.rglob(f"*{ext.upper()}")))
            elif p.is_file():
                found.append(p)

        added = skipped = 0
        for p in found:
            if p.suffix.lower() not in EXT_MAP:
                self.log_line(f"跳过（不是加密音乐格式）：{p.name}")
                skipped += 1
                continue
            key = str(p.resolve()).lower()
            if key in self.by_path:
                continue
            try:
                size = human_size(p.stat().st_size)
            except OSError:
                size = "?"
            job = Job(src=p)
            iid = self.tree.insert("", "end", values=(p.name, size, job.status, ""))
            job.iid = iid
            self.jobs.append(job)
            self.by_path[key] = job
            added += 1

        if added:
            self.log_line(f"已加入 {added} 个文件" + (f"（跳过 {skipped} 个）" if skipped else ""))
        elif found:
            self.log_line("没有新增文件（可能已在列表中）。")
        else:
            self.log_line("没有找到可处理的加密音乐文件。")
        self._update_progress()

    def remove_selected(self) -> None:
        for iid in list(self.tree.selection()):
            job = next((j for j in self.jobs if j.iid == iid), None)
            if job:
                self.jobs.remove(job)
                self.by_path.pop(str(job.src.resolve()).lower(), None)
            self.tree.delete(iid)
        self._update_progress()

    def clear_all(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.jobs.clear()
        self.by_path.clear()
        self._update_progress()

    def _update_progress(self) -> None:
        self.pb.configure(maximum=max(len(self.jobs), 1), value=0)
        self.var_progress.set(f"共 {len(self.jobs)} 个文件")

    # ------------------------------------------------------------------ 转换
    def _out_dir_for(self, src: Path) -> Path:
        if self.var_outmode.get() == "custom":
            base = Path(self.var_outdir.get() or (BASE_DIR / "output"))
            return base if base.is_absolute() else BASE_DIR / base
        return src.parent / "output"

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        pending = [j for j in self.jobs if j.status in ("等待", "失败", "已取消")]
        if not pending:
            messagebox.showinfo(APP_TITLE, "没有待处理的文件。")
            return
        if self.var_format.get() in (FORMAT_MP3, FORMAT_BOTH):
            ff = transcoder.find_ffmpeg(self.var_ffmpeg.get() or None)
            if not ff:
                messagebox.showerror(APP_TITLE, "需要 ffmpeg 才能生成 MP3，但没有找到。\n\n"
                                                "请点「指定…」选择 ffmpeg.exe。")
                return
            if not transcoder.check_mp3_support(ff):
                messagebox.showerror(APP_TITLE, "这个 ffmpeg 不支持 libmp3lame，无法生成 MP3。\n\n"
                                                "请换一个完整版 ffmpeg。")
                return

        self._save_config()
        self.stop_event.clear()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        for b in self._edit_buttons:
            b.configure(state="disabled")
        self.worker = threading.Thread(target=self._run_jobs, args=(pending,), daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.log_line("已请求停止，将在当前文件处理完后中断…")

    def _run_jobs(self, pending: List[Job]) -> None:
        total = len(pending)
        done = 0
        ok_n = 0
        fail_n = 0
        skip_n = 0
        mode = self.var_format.get()
        bitrate = self.var_bitrate.get()
        skip = self.var_skip.get()
        del_mid = self.var_delmid.get()
        ff = transcoder.find_ffmpeg(self.var_ffmpeg.get() or None)

        try:
            self.decryptor.attach()
        except Exception as exc:
            self.post("log", f"无法连接 QQ音乐：{exc}")
            self.post("log", "请确认 QQ音乐正在运行，然后点「重新连接」。")
            for j in pending:
                self.post("status", j.iid, "已取消", "err")
            self.post("progress", 0, total, "已中断")
            self.post("done")
            return

        for job in pending:
            if self.stop_event.is_set():
                self.post("status", job.iid, "已取消", "skip")
                skip_n += 1
                continue

            src = job.src
            self.post("status", job.iid, "处理中…", "run")
            self.post("progress", done, total, f"正在处理 {done + 1}/{total}：{src.name}")

            try:
                out_dir = self._out_dir_for(src)
                out_dir.mkdir(parents=True, exist_ok=True)

                # 1) 原生文件（mgg -> ogg / mflac -> flac）
                predicted = out_dir / (src.stem + native_extension(src))
                native_path: Optional[Path] = None

                need_native = mode in (FORMAT_NATIVE, FORMAT_BOTH, FORMAT_MP3)
                if skip and predicted.exists() and predicted.stat().st_size > 0:
                    native_path = predicted
                    self.post("log", f"已存在，跳过解密：{predicted.name}")
                elif need_native:
                    data = self.decryptor.decrypt(src)
                    ext = sniff_extension(data, native_extension(src))
                    native_path = out_dir / (src.stem + ext)
                    tmp = native_path.with_name(native_path.name + ".part")
                    tmp.write_bytes(data)
                    os.replace(tmp, native_path)
                    self.post("log", f"解密完成：{src.name} → {native_path.name} "
                                     f"({human_size(len(data))}, {ext})")

                # 2) MP3
                if mode in (FORMAT_MP3, FORMAT_BOTH) and native_path:
                    mp3_path = out_dir / (src.stem + ".mp3")
                    if skip and mp3_path.exists() and mp3_path.stat().st_size > 0:
                        self.post("log", f"已存在，跳过转码：{mp3_path.name}")
                        job.outputs.append(mp3_path)
                    else:
                        self.post("status", job.iid, "转 MP3…", "run")
                        ok, err = transcoder.to_mp3(native_path, mp3_path, bitrate, ff)
                        if not ok:
                            raise DecryptError(f"转 MP3 失败：{err}")
                        job.outputs.append(mp3_path)
                        self.post("log", f"转码完成：{native_path.name} → {mp3_path.name} ({bitrate})")

                    if mode == FORMAT_MP3 and del_mid:
                        try:
                            native_path.unlink()
                            self.post("log", f"已删除中间文件：{native_path.name}")
                        except OSError:
                            pass

                if native_path:
                    job.outputs.append(native_path)
                elif mode == FORMAT_MP3:
                    job.outputs.append(out_dir / (src.stem + ".mp3"))

                names = "、".join(p.name for p in dict.fromkeys(job.outputs))
                self.post("status", job.iid, "完成", "ok")
                self.post("output", job.iid, names)
                ok_n += 1

            except (DecryptError, QQMusicNotRunning) as exc:
                job.error = str(exc)
                fail_n += 1
                self.post("status", job.iid, "失败", "err")
                self.post("output", job.iid, f"错误：{exc}")
                self.post("log", f"失败：{src.name} —— {exc}")
            except Exception as exc:  # noqa: BLE001
                job.error = str(exc)
                fail_n += 1
                self.post("status", job.iid, "失败", "err")
                self.post("output", job.iid, f"错误：{exc}")
                self.post("log", f"失败：{src.name} —— {exc}\n{traceback.format_exc()}")

            done += 1
            self.post("progress", done, total, f"已完成 {done}/{total}")

        summary = f"—— 全部结束：成功 {ok_n} 个，失败 {fail_n} 个"
        if skip_n:
            summary += f"，跳过 {skip_n} 个"
        summary += " ——"
        self.post("log", summary)
        self.post("progress", total, total, f"完成 {ok_n}/{total}")
        self.post("done")

    # ------------------------------------------------------------------ 其它
    def open_output_dir(self) -> None:
        if self.var_outmode.get() == "custom":
            d = Path(self.var_outdir.get())
        elif self.jobs:
            d = self.jobs[0].src.parent / "output"
        else:
            d = BASE_DIR / "output"
        try:
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法打开目录：{d}\n{exc}")

    def _load_config(self) -> None:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        self.var_format.set(cfg.get("format", self.var_format.get()))
        self.var_bitrate.set(cfg.get("bitrate", self.var_bitrate.get()))
        self.var_outmode.set(cfg.get("outmode", self.var_outmode.get()))
        self.var_outdir.set(cfg.get("outdir", self.var_outdir.get()))
        self.var_skip.set(bool(cfg.get("skip", True)))
        self.var_delmid.set(bool(cfg.get("delmid", False)))
        self.var_ffmpeg.set(cfg.get("ffmpeg", ""))

    def _save_config(self) -> None:
        cfg = {
            "format": self.var_format.get(),
            "bitrate": self.var_bitrate.get(),
            "outmode": self.var_outmode.get(),
            "outdir": self.var_outdir.get(),
            "skip": self.var_skip.get(),
            "delmid": self.var_delmid.get(),
            "ffmpeg": self.var_ffmpeg.get(),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self) -> None:
        self.stop_event.set()
        self._save_config()
        try:
            self.decryptor.detach()
        except Exception:
            pass
        self.root.destroy()


# --------------------------------------------------------------------------- 入口
def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            windll.user32.SetProcessDPIAware()  # type: ignore[name-defined]
        except Exception:
            pass


def main() -> int:
    enable_dpi_awareness()
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        crash = BASE_DIR / "crash.log"
        crash.write_text(traceback.format_exc(), encoding="utf-8")
        raise
