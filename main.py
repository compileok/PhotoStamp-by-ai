"""照片时间水印工具 — 桌面 GUI 入口。

完全本地运行，无网络依赖。
启动：python3 main.py
"""

import os
import re
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import watermark as wm
import geo

# 拖拽支持（可选依赖：未安装 tkinterdnd2 时自动降级为仅按钮选择）
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    DND_OK = True
except ImportError:
    DND_OK = False

TIME_FORMATS = [
    ("2026-08-15 10:44", "%Y-%m-%d %H:%M"),
    ("2026/08/15 10:44", "%Y/%m/%d %H:%M"),
    ("2026年08月15日 10:44", "%Y年%m月%d日 %H:%M"),
    ("2026-08-15", "%Y-%m-%d"),
    ("08-15 10:44", "%m-%d %H:%M"),
]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("照片时间水印工具")
        self.root.geometry("780x620")
        self.root.minsize(680, 520)

        self.files: list[str] = []
        self.processing = False

        self._build_style()
        self._build_ui()

    # ---------- UI ----------
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("aqua")
        except tk.TclError:
            pass
        # Tk 不支持字体族列表，动态选一个可用的中文字体
        import tkinter.font as tkfont

        families = set(tkfont.families(self.root))
        family = next((f for f in ("PingFang SC", "Hiragino Sans GB", "Heiti SC", "Helvetica") if f in families), "Helvetica")
        base = (family, 13)
        self.root.option_add("*Font", base)
        style.configure("Treeview", rowheight=28, font=base)
        style.configure("TButton", padding=(12, 6))
        style.configure("Start.TButton", font=base + ("bold",), padding=(20, 8))

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # 顶部按钮区
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Button(top, text="选择图片", command=self.pick_files).pack(side="left", padx=(0, 8))
        ttk.Button(top, text="选择文件夹", command=self.pick_folder).pack(side="left", padx=(0, 8))
        ttk.Button(top, text="清空列表", command=self.clear_list).pack(side="left")
        self.count_var = tk.StringVar(value="未选择照片")
        ttk.Label(top, textvariable=self.count_var).pack(side="right")

        # 文件列表
        list_frame = ttk.LabelFrame(
            self.root,
            text="照片列表（自动读取拍摄时间 / 地点）" + (" — 支持拖拽图片或文件夹到此" if DND_OK else ""),
        )
        list_frame.pack(fill="both", expand=True, **pad)

        cols = ("file", "time", "location", "source")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("file", text="文件")
        self.tree.heading("time", text="拍摄时间")
        self.tree.heading("location", text="地点")
        self.tree.heading("source", text="时间来源")
        self.tree.column("file", width=260)
        self.tree.column("time", width=150)
        self.tree.column("location", width=180)
        self.tree.column("source", width=90, anchor="center")
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # 参数区
        opt = ttk.LabelFrame(self.root, text="水印设置")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="位置:").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.position_var = tk.StringVar(value="右下角")
        ttk.Combobox(opt, textvariable=self.position_var, values=wm.POSITIONS,
                     state="readonly", width=10).grid(row=0, column=1, sticky="w", pady=8)
        ttk.Label(opt, text="    字号:").grid(row=0, column=2, sticky="w", pady=8)
        self.fontsize_var = tk.StringVar(value="自动")
        ttk.Combobox(opt, textvariable=self.fontsize_var,
                     values=list(wm.FONT_SIZES.keys()),
                     state="readonly", width=8).grid(row=0, column=3, sticky="w", pady=8)
        ttk.Label(opt, text="    时间格式:").grid(row=0, column=4, sticky="w", pady=8)
        self.format_var = tk.StringVar(value=TIME_FORMATS[0][1])
        fmt_box = ttk.Combobox(opt, textvariable=self.format_var, width=22,
                               values=[f[1] for f in TIME_FORMATS])
        fmt_box.grid(row=0, column=5, sticky="w", pady=8)
        fmt_box.bind("<<ComboboxSelected>>", lambda e: self.refresh_times())

        # 显示地点开关（默认开）
        self.loc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="显示地点（需照片带 GPS，完全离线匹配到区县）",
                        variable=self.loc_var, command=self.refresh_list)\
            .grid(row=1, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 8))

        # 自定义水印文字（EXIF 丢失时手动补记，优先于自动读取）
        self.custom_var = tk.StringVar()
        ttk.Label(opt, text="自定义水印:").grid(row=2, column=0, sticky="w", padx=8, pady=(0, 8))
        custom_entry = ttk.Entry(opt, textvariable=self.custom_var)
        custom_entry.grid(row=2, column=1, columnspan=3, sticky="we", pady=(0, 8))
        ttk.Label(opt, text="填写后优先于自动读取（EXIF 丢失时手动补记）")\
            .grid(row=2, column=4, columnspan=2, sticky="w", pady=(0, 8))
        opt.columnconfigure(1, weight=1)

        # 进度 + 开始按钮
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 12))
        self.start_btn = ttk.Button(bottom, text="开始处理", style="Start.TButton", command=self.start)
        self.start_btn.pack(side="right")

        # 日志
        log_frame = ttk.LabelFrame(self.root, text="处理日志")
        log_frame.pack(fill="x", padx=12, pady=(6, 12))
        self.log_text = tk.Text(log_frame, height=5, state="disabled",
                                font=("Menlo", 11), background="#f5f5f5")
        self.log_text.pack(fill="x", padx=4, pady=4)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(fill="x", side="bottom")

        self._enable_dnd()

    # ---------- 拖拽 ----------
    def _enable_dnd(self):
        """注册拖拽放下目标（需 tkinterdnd2，未安装则跳过）。"""
        if not DND_OK:
            return
        for widget in (self.root, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._on_drop)

    @staticmethod
    def _parse_dnd_paths(data: str) -> list[str]:
        """解析 tkdnd 事件数据。含空格的路径被 {} 包裹，其余按空白分割。"""
        paths = []
        for m in re.findall(r"\{[^}]*\}|[^\s]+", data):
            paths.append(m[1:-1] if m.startswith("{") else m)
        return [p for p in paths if p]

    def _on_drop(self, event):
        files, dirs, skipped = [], [], 0
        for p in self._parse_dnd_paths(event.data):
            if os.path.isdir(p):
                dirs.append(p)
            elif wm.is_supported(p):
                files.append(p)
            else:
                skipped += 1
        if dirs:
            for d in dirs:
                found = wm.scan_folder(d)
                self.log(f"拖入文件夹 {d}：找到 {len(found)} 张图片")
                files.extend(found)
        if files:
            self.add_files(files)
        if skipped:
            self.log(f"已忽略 {skipped} 个不支持的文件")

    # ---------- 逻辑 ----------
    def log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def add_files(self, paths: list[str]):
        existing = set(self.files)
        for p in paths:
            if p not in existing:
                self.files.append(p)
        self.refresh_list()

    def _location_of(self, path: str) -> str:
        """读 GPS 并匹配区县，返回地点文本或 "—"。"""
        gps = wm.get_gps(path)
        if not gps:
            return "—"
        return geo.lookup(*gps) or "—"

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for p in self.files:
            t, source = wm.get_capture_time(p)
            time_str = t.strftime("%Y-%m-%d %H:%M:%S") if t else "—"
            # 时间格式选项变化时预览列直接用当前格式
            try:
                if t:
                    time_str = t.strftime(self.format_var.get())
            except Exception:
                pass
            location = self._location_of(p) if self.loc_var.get() else ""
            self.tree.insert("", "end", values=(p, time_str, location, source))
        n = len(self.files)
        self.count_var.set(f"共 {n} 张" if n else "未选择照片")

    def refresh_times(self):
        self.refresh_list()

    def pick_files(self):
        types = [("图片", " ".join(f"*{e}" for e in sorted(wm.SUPPORTED_EXTENSIONS))),
                 ("所有文件", "*.*")]
        paths = filedialog.askopenfilenames(title="选择图片", filetypes=types)
        if paths:
            self.add_files(list(paths))

    def pick_folder(self):
        folder = filedialog.askdirectory(title="选择照片文件夹")
        if not folder:
            return
        files = wm.scan_folder(folder)
        if not files:
            messagebox.showinfo("提示", "该文件夹（及子文件夹）中没有找到支持的图片。")
            return
        self.add_files(files)
        self.log(f"扫描 {folder}：找到 {len(files)} 张图片")

    def clear_list(self):
        self.files.clear()
        self.refresh_list()

    def start(self):
        if self.processing:
            return
        if not self.files:
            messagebox.showwarning("提示", "请先选择照片。")
            return

        first_dir = os.path.dirname(self.files[0])
        output_dir = os.path.join(first_dir, "watermarked")

        self.processing = True
        self.start_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=len(self.files))
        self.status_var.set(f"处理中 → 输出目录: {output_dir}")
        self.log(f"开始处理 {len(self.files)} 张，输出到 {output_dir}")

        fmt = self.format_var.get()
        pos = self.position_var.get()
        files = self.files[:]
        show_loc = self.loc_var.get()
        font_scale = wm.FONT_SIZES.get(self.fontsize_var.get())
        custom_text = self.custom_var.get().strip()

        def location_provider(path):
            gps = wm.get_gps(path)
            return geo.lookup(*gps) if gps else None

        def worker():
            results = wm.process_batch(
                files, output_dir, fmt, pos,
                show_location=show_loc,
                location_provider=location_provider,
                font_scale=font_scale,
                custom_text=custom_text or None,
                progress_callback=progress_cb,
            )
            self.root.after(0, lambda: self.done(results, output_dir))

        def progress_cb(done, total):
            self.root.after(0, lambda d=done: self._set_progress(d))

        threading.Thread(target=worker, daemon=True).start()

    def _set_progress(self, done: int):
        self.progress.configure(value=done)
        self.status_var.set(f"已处理 {done} / {self.progress.cget('maximum')}")

    def done(self, results, output_dir: str):
        ok = sum(1 for _, s, _ in results if s)
        fail = len(results) - ok
        for path, success, info in results:
            name = os.path.basename(path)
            if success:
                self.log(f"  ✓ {name}")
            else:
                self.log(f"  ✗ {name} — {info}")
        self.log(f"完成：成功 {ok}，失败 {fail}，输出目录 {output_dir}\n")
        self.status_var.set(f"完成 ✓ {ok} 张成功" + (f"，✗ {fail} 张失败" if fail else ""))
        self.processing = False
        self.start_btn.configure(state="normal")
        if ok > 0:
            # 有成功输出就自动打开目录（用 subprocess 传参，避免路径特殊字符问题）
            self.root.after(400, lambda: self._open_dir(output_dir))

    @staticmethod
    def _open_dir(path: str):
        import subprocess
        import sys

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform.startswith("win"):
                subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError:
            pass


def main():
    root = TkinterDnD.Tk() if DND_OK else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
