"""CustomTkinter front end for FPS Booster."""

from __future__ import annotations

import queue
import threading
from collections import Counter
from typing import Callable

import customtkinter as ctk
import psutil

from .core import (
    MAX_PRESETS,
    KillReport,
    Preset,
    PresetStore,
    ProcessGroup,
    is_protected,
    kill_processes,
    list_process_groups,
)
from .icons import get_icon

# ---- Theme ---------------------------------------------------------------
BG = "#0b0d12"
PANEL = "#141821"
PANEL_HI = "#1b2030"
BORDER = "#262c3d"
ACCENT = "#00e59b"
ACCENT_HOVER = "#00b97d"
DANGER = "#ff4d5e"
DANGER_HOVER = "#d93a4a"
WARN = "#ffb547"
TEXT = "#e8ecf4"
MUTED = "#8a93a8"

MEMORY_FILTERS = {"All": 0, "≥ 25 MB": 25, "≥ 100 MB": 100, "≥ 250 MB": 250}
SORT_OPTIONS = ("A–Z", "Memory")
MAX_ROWS = 200
ICON_SIZE = 20


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def fmt_mb(mb: float) -> str:
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def run_in_thread(root: ctk.CTk, fn: Callable, on_done: Callable[[bool, object], None]) -> None:
    """Run ``fn`` off the UI thread, then call ``on_done(ok, result)`` on it."""
    results: queue.Queue = queue.Queue()

    def worker():
        try:
            results.put((True, fn()))
        except Exception as exc:  # surfaced to the UI, not swallowed
            results.put((False, exc))

    def poll():
        try:
            ok, value = results.get_nowait()
        except queue.Empty:
            root.after(40, poll)
            return
        on_done(ok, value)

    threading.Thread(target=worker, daemon=True).start()
    root.after(40, poll)


# ---- Dialogs -------------------------------------------------------------


class Dialog(ctk.CTkToplevel):
    def __init__(self, master, title: str, width: int = 460, height: int = 420):
        super().__init__(master, fg_color=BG)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height) // 3
        self.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")
        self.after(10, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_force()
        except Exception:
            pass  # window not viewable yet / already closed


class ConfirmDialog(Dialog):
    def __init__(self, master, title: str, message: str, confirm_text: str = "Yes"):
        super().__init__(master, title, 400, 190)
        self.result = False
        ctk.CTkLabel(self, text=title, font=font(18, "bold"), text_color=TEXT).pack(
            anchor="w", padx=24, pady=(22, 4)
        )
        ctk.CTkLabel(
            self, text=message, font=font(13), text_color=MUTED, wraplength=350, justify="left"
        ).pack(anchor="w", padx=24)
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(side="bottom", fill="x", padx=24, pady=20)
        ctk.CTkButton(
            row, text=confirm_text, width=110, fg_color=DANGER, hover_color=DANGER_HOVER,
            font=font(13, "bold"), command=self._yes,
        ).pack(side="right")
        ctk.CTkButton(
            row, text="Cancel", width=90, fg_color=PANEL_HI, hover_color=BORDER,
            font=font(13), command=self.destroy,
        ).pack(side="right", padx=8)

    def _yes(self):
        self.result = True
        self.destroy()


class BoostResultDialog(Dialog):
    def __init__(self, master, preset_name: str, report: KillReport):
        super().__init__(master, "Boost complete", 480, 460)
        head = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=14)
        head.pack(fill="x", padx=20, pady=(20, 12))
        ctk.CTkLabel(
            head, text=f"⚡ {preset_name}", font=font(14, "bold"), text_color=MUTED
        ).pack(anchor="w", padx=18, pady=(14, 0))
        n = len(report.killed)
        ctk.CTkLabel(
            head, text=f"{fmt_mb(report.freed_mb)} freed" if n else "Already clean",
            font=font(30, "bold"), text_color=ACCENT,
        ).pack(anchor="w", padx=18)
        ctk.CTkLabel(
            head,
            text=f"Closed {n} process{'es' if n != 1 else ''}" if n else "None of these processes were running",
            font=font(13), text_color=TEXT,
        ).pack(anchor="w", padx=18, pady=(0, 14))

        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        def section(title: str, color: str, lines: list[str]):
            if not lines:
                return
            ctk.CTkLabel(body, text=title, font=font(13, "bold"), text_color=color).pack(
                anchor="w", padx=8, pady=(8, 2)
            )
            for line in lines:
                ctk.CTkLabel(
                    body, text=line, font=font(12), text_color=TEXT, wraplength=400, justify="left"
                ).pack(anchor="w", padx=20)

        counts = Counter(report.killed)
        section("✔ Closed", ACCENT, [f"{name}  ×{c}" if c > 1 else name for name, c in counts.items()])
        section("✖ Couldn't close", DANGER, [f"{n} — {why}" for n, why in report.failed.items()])
        section("○ Not running", MUTED, report.not_running)
        section("⛨ Skipped (system process)", WARN, report.skipped_protected)

        ctk.CTkButton(
            self, text="Nice", height=38, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#04130d", font=font(14, "bold"), command=self.destroy,
        ).pack(fill="x", padx=20, pady=16)


# ---- Home view: the 5 preset slots ---------------------------------------


class HomeView(ctk.CTkFrame):
    def __init__(self, app: "FPSBoosterApp"):
        super().__init__(app.body, fg_color="transparent")
        self.app = app
        self.buttons: list[ctk.CTkButton] = []

        ctk.CTkLabel(
            self, text="Your presets", font=font(18, "bold"), text_color=TEXT
        ).pack(anchor="w", padx=4)
        ctk.CTkLabel(
            self,
            text="Hit BOOST to close every process in a preset, or Edit to change what it closes.",
            font=font(13), text_color=MUTED,
        ).pack(anchor="w", padx=4, pady=(0, 14))

        self.grid_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.grid_frame.pack(fill="both", expand=True)
        for col in range(MAX_PRESETS):
            self.grid_frame.grid_columnconfigure(col, weight=1, uniform="slot")
        self.grid_frame.grid_rowconfigure(0, weight=1)

        self.status = ctk.CTkLabel(self, text="", font=font(13), text_color=MUTED)
        self.status.pack(anchor="w", padx=4, pady=(12, 0))
        self.refresh()

    def refresh(self):
        for child in self.grid_frame.winfo_children():
            child.destroy()
        self.buttons.clear()
        for i, preset in enumerate(self.app.store.slots):
            card = ctk.CTkFrame(
                self.grid_frame, fg_color=PANEL, corner_radius=16,
                border_width=1, border_color=BORDER,
            )
            card.grid(row=0, column=i, sticky="nsew", padx=6)
            if preset:
                self._filled_card(card, i, preset)
            else:
                self._empty_card(card, i)

    def _filled_card(self, card, index: int, preset: Preset):
        ctk.CTkLabel(card, text=f"PRESET {index + 1}", font=font(11, "bold"), text_color=ACCENT).pack(
            anchor="w", padx=16, pady=(16, 0)
        )
        ctk.CTkLabel(
            card, text=preset.name, font=font(17, "bold"), text_color=TEXT,
            wraplength=150, justify="left",
        ).pack(anchor="w", padx=16)
        n = len(preset.processes)
        ctk.CTkLabel(
            card, text=f"{n} process{'es' if n != 1 else ''}", font=font(12), text_color=MUTED
        ).pack(anchor="w", padx=16, pady=(0, 8))

        preview = preset.processes[:7]
        extra = len(preset.processes) - len(preview)
        text = "\n".join(f"• {p}" for p in preview) + (f"\n+ {extra} more" if extra > 0 else "")
        ctk.CTkLabel(
            card, text=text, font=font(12), text_color=MUTED, justify="left", anchor="nw",
            wraplength=150,
        ).pack(anchor="nw", padx=16, fill="both", expand=True)

        edit = ctk.CTkButton(
            card, text="✎  Edit", height=32, fg_color=PANEL_HI, hover_color=BORDER,
            font=font(13), command=lambda: self.app.show_editor(index),
        )
        edit.pack(side="bottom", fill="x", padx=14, pady=(6, 14))
        boost = ctk.CTkButton(
            card, text="⚡ BOOST", height=44, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            text_color="#04130d", font=font(15, "bold"),
            command=lambda: self.boost(index),
        )
        boost.pack(side="bottom", fill="x", padx=14)
        self.buttons += [edit, boost]

    def _empty_card(self, card, index: int):
        ctk.CTkLabel(card, text=f"PRESET {index + 1}", font=font(11, "bold"), text_color=MUTED).pack(
            anchor="w", padx=16, pady=(16, 0)
        )
        ctk.CTkLabel(card, text="Empty slot", font=font(17, "bold"), text_color=MUTED).pack(
            anchor="w", padx=16
        )
        new = ctk.CTkButton(
            card, text="+  Create", height=44, fg_color="transparent", border_width=2,
            border_color=BORDER, hover_color=PANEL_HI, text_color=TEXT, font=font(14, "bold"),
            command=lambda: self.app.show_editor(index),
        )
        new.pack(side="bottom", fill="x", padx=14, pady=14)
        self.buttons.append(new)

    def boost(self, index: int):
        preset = self.app.store.slots[index]
        if not preset:
            return
        for b in self.buttons:
            b.configure(state="disabled")
        self.status.configure(text=f"⚡ Boosting with “{preset.name}”…", text_color=ACCENT)

        def done(ok: bool, result):
            for b in self.buttons:
                b.configure(state="normal")
            if not ok:
                self.status.configure(text=f"Boost failed: {result}", text_color=DANGER)
                return
            self.status.configure(
                text=f"Last boost: “{preset.name}” closed {len(result.killed)} processes, "
                f"freed {fmt_mb(result.freed_mb)}",
                text_color=MUTED,
            )
            BoostResultDialog(self.app, preset.name, result)

        run_in_thread(self.app, lambda: kill_processes(preset.processes), done)


# ---- Editor view: tick the processes a preset should kill ----------------


class EditorView(ctk.CTkFrame):
    def __init__(self, app: "FPSBoosterApp", index: int):
        super().__init__(app.body, fg_color="transparent")
        self.app = app
        self.index = index
        existing = app.store.slots[index]
        # lowercase name -> display name, in the order they were ticked
        self.selected: dict[str, str] = {p.lower(): p for p in (existing.processes if existing else [])}
        self.groups: list[ProcessGroup] = []
        self._search_job = None

        # Top bar
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x")
        ctk.CTkButton(
            top, text="← Back", width=80, height=34, fg_color=PANEL_HI, hover_color=BORDER,
            font=font(13), command=app.show_home,
        ).pack(side="left")
        ctk.CTkLabel(
            top, text=f"{'Edit' if existing else 'New'} preset {index + 1}",
            font=font(18, "bold"), text_color=TEXT,
        ).pack(side="left", padx=14)
        self.name_entry = ctk.CTkEntry(
            top, placeholder_text="Preset name (e.g. Valorant)", height=34, width=260,
            fg_color=PANEL, border_color=BORDER, font=font(13),
        )
        self.name_entry.pack(side="right")
        if existing:
            self.name_entry.insert(0, existing.name)

        # Toolbar
        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12)
        bar.pack(fill="x", pady=(14, 8))
        # No textvariable: CTkEntry hides its placeholder when one is set.
        self.search = ctk.CTkEntry(
            bar, placeholder_text="🔍  Search processes",
            width=230, height=32, fg_color=BG, border_color=BORDER, font=font(13),
        )
        self.search.pack(side="left", padx=10, pady=10)
        self.search.bind("<KeyRelease>", lambda _: self._debounced_render())
        self.filter_var = ctk.StringVar(value="≥ 25 MB")
        ctk.CTkSegmentedButton(
            bar, values=list(MEMORY_FILTERS), variable=self.filter_var,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            text_color=TEXT, font=font(12), command=lambda _: self.render(),
        ).pack(side="left", padx=4)
        self.sort_var = ctk.StringVar(value=SORT_OPTIONS[0])
        ctk.CTkSegmentedButton(
            bar, values=list(SORT_OPTIONS), variable=self.sort_var,
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER,
            text_color=TEXT, font=font(12), command=lambda _: self.render(),
        ).pack(side="left", padx=(10, 4))
        self.only_selected = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            bar, text="Ticked only", variable=self.only_selected, progress_color=ACCENT,
            font=font(12), text_color=TEXT, command=self.render,
        ).pack(side="left", padx=12)
        self.refresh_btn = ctk.CTkButton(
            bar, text="⟳ Refresh", width=96, height=32, fg_color=PANEL_HI, hover_color=BORDER,
            font=font(13), command=self.scan,
        )
        self.refresh_btn.pack(side="right", padx=10)

        # Column headers
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=12)
        self._columns(header)
        for col, text in enumerate(("", "PROCESS", "RUNNING", "MEMORY", "")):
            ctk.CTkLabel(header, text=text, font=font(11, "bold"), text_color=MUTED, anchor="w").grid(
                row=0, column=col, sticky="w", padx=6
            )

        self.list_frame = ctk.CTkScrollableFrame(
            self, fg_color=PANEL, corner_radius=12, border_width=1, border_color=BORDER
        )
        self.list_frame.pack(fill="both", expand=True, pady=(2, 10))
        self._columns(self.list_frame)

        # Footer
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x")
        self.manual_entry = ctk.CTkEntry(
            foot, placeholder_text="Add by name, e.g. Discord.exe", width=220, height=34,
            fg_color=PANEL, border_color=BORDER, font=font(13),
        )
        self.manual_entry.pack(side="left")
        self.manual_entry.bind("<Return>", lambda _: self.add_manual())
        ctk.CTkButton(
            foot, text="+ Add", width=64, height=34, fg_color=PANEL_HI, hover_color=BORDER,
            font=font(13), command=self.add_manual,
        ).pack(side="left", padx=6)
        self.count_label = ctk.CTkLabel(foot, text="", font=font(13, "bold"), text_color=ACCENT)
        self.count_label.pack(side="left", padx=12)

        ctk.CTkButton(
            foot, text="💾  Save preset", width=140, height=38, fg_color=ACCENT,
            hover_color=ACCENT_HOVER, text_color="#04130d", font=font(14, "bold"),
            command=self.save,
        ).pack(side="right")
        if existing:
            ctk.CTkButton(
                foot, text="Delete", width=80, height=38, fg_color="transparent",
                border_width=1, border_color=DANGER, text_color=DANGER, hover_color=PANEL_HI,
                font=font(13), command=self.delete,
            ).pack(side="right", padx=8)
        self.message = ctk.CTkLabel(self, text="", font=font(12), text_color=DANGER)
        self.message.pack(anchor="e", pady=(4, 0))

        self._update_count()
        self.scan()

    @staticmethod
    def _columns(frame):
        frame.grid_columnconfigure(0, minsize=36)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_columnconfigure(2, minsize=80)
        frame.grid_columnconfigure(3, minsize=90)
        frame.grid_columnconfigure(4, minsize=170)

    # -- data --

    def scan(self):
        self.refresh_btn.configure(state="disabled", text="Scanning…")

        def work():
            groups = list_process_groups(0)
            # Icon extraction is the slow part, so it happens here, off the UI thread.
            return groups, {g.name.lower(): get_icon(g.name, g.exe) for g in groups}

        def done(ok: bool, result):
            if not self.winfo_exists():
                return
            self.refresh_btn.configure(state="normal", text="⟳ Refresh")
            if ok:
                self.groups, icons = result
                for key, image in icons.items():
                    self.app.set_icon(key, image)
                self.render()
            else:
                self.message.configure(text=f"Couldn't read processes: {result}")

        run_in_thread(self.app, work, done)

    # -- rendering --

    def _debounced_render(self):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(180, self.render)

    def render(self):
        self._search_job = None
        for child in self.list_frame.winfo_children():
            child.destroy()

        query = self.search.get().strip().lower()
        min_mb = MEMORY_FILTERS[self.filter_var.get()]
        only_sel = self.only_selected.get()
        running = {g.name.lower() for g in self.groups}

        rows: list[tuple[str, ProcessGroup | None]] = []
        # Ticked processes that aren't open right now stay visible so they
        # can still be unticked.
        for key, name in self.selected.items():
            if key not in running and query in key:
                rows.append((name, None))
        for g in self.groups:
            key = g.name.lower()
            if query and query not in key:
                continue
            if only_sel and key not in self.selected:
                continue
            if g.memory_mb < min_mb and key not in self.selected:
                continue
            rows.append((g.name, g))

        if self.sort_var.get() == "Memory":
            rows.sort(key=lambda row: row[1].memory_bytes if row[1] else -1, reverse=True)
        else:
            rows.sort(key=lambda row: row[0].lower())

        top_mb = max((g.memory_mb for g in self.groups), default=1) or 1
        for r, (name, group) in enumerate(rows[:MAX_ROWS]):
            self._row(r, name, group, top_mb)

        if not rows:
            ctk.CTkLabel(
                self.list_frame, text="No matching processes", font=font(13), text_color=MUTED
            ).grid(row=0, column=0, columnspan=5, pady=30)
        elif len(rows) > MAX_ROWS:
            ctk.CTkLabel(
                self.list_frame, text=f"+ {len(rows) - MAX_ROWS} more — use search to narrow down",
                font=font(12), text_color=MUTED,
            ).grid(row=MAX_ROWS, column=0, columnspan=5, pady=8)

    def _row(self, r: int, name: str, group: ProcessGroup | None, top_mb: float):
        key = name.lower()
        ticked = key in self.selected
        pad = {"pady": 3, "padx": 6}

        box = ctk.CTkCheckBox(
            self.list_frame, text="", width=24, checkbox_width=20, checkbox_height=20,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=MUTED,
            command=lambda: self.toggle(name),
        )
        if ticked:
            box.select()
        box.grid(row=r, column=0, sticky="w", **pad)

        label = ctk.CTkLabel(
            self.list_frame, text=f"  {name}", anchor="w", compound="left",
            image=self.app.icon_for(name), font=font(13, "bold" if ticked else "normal"),
            text_color=TEXT if ticked or group else MUTED, cursor="hand2",
        )
        label.grid(row=r, column=1, sticky="ew", **pad)
        label.bind("<Button-1>", lambda _: (box.toggle()))

        if group is None:
            ctk.CTkLabel(self.list_frame, text="not running", font=font(12), text_color=MUTED,
                         anchor="w").grid(row=r, column=2, columnspan=3, sticky="w", **pad)
            return

        ctk.CTkLabel(self.list_frame, text=f"×{group.count}", font=font(12), text_color=MUTED,
                     anchor="w").grid(row=r, column=2, sticky="w", **pad)
        ctk.CTkLabel(self.list_frame, text=fmt_mb(group.memory_mb), font=font(12, "bold"),
                     text_color=TEXT, anchor="w").grid(row=r, column=3, sticky="w", **pad)
        ratio = group.memory_mb / top_mb
        bar = ctk.CTkProgressBar(
            self.list_frame, width=150, height=8, fg_color=PANEL_HI,
            progress_color=DANGER if ratio > 0.5 else WARN if ratio > 0.2 else ACCENT,
        )
        bar.set(ratio)
        bar.grid(row=r, column=4, sticky="w", **pad)

    # -- actions --

    def toggle(self, name: str):
        key = name.lower()
        if key in self.selected:
            del self.selected[key]
        else:
            self.selected[key] = name
        self._update_count()
        if self.only_selected.get():
            self.render()

    def add_manual(self):
        name = self.manual_entry.get().strip()
        if not name:
            return
        if is_protected(name):
            self.message.configure(text=f"{name} is a system process and can't be added.")
            return
        # Reuse the real spelling if it's running.
        match = next((g.name for g in self.groups if g.name.lower() == name.lower()), name)
        self.selected.setdefault(match.lower(), match)
        self.manual_entry.delete(0, "end")
        self.message.configure(text="")
        self._update_count()
        self.render()

    def _update_count(self):
        n = len(self.selected)
        self.count_label.configure(text=f"{n} ticked" if n else "Nothing ticked yet")

    def save(self):
        if not self.selected:
            self.message.configure(text="Tick at least one process for this preset to close.")
            return
        name = self.name_entry.get().strip() or f"Preset {self.index + 1}"
        self.app.store.set(self.index, Preset(name=name, processes=list(self.selected.values())))
        self.app.show_home()

    def delete(self):
        dialog = ConfirmDialog(
            self.app, "Delete preset?",
            f"“{self.app.store.slots[self.index].name}” will be removed. This can't be undone.",
            confirm_text="Delete",
        )
        self.app.wait_window(dialog)
        if dialog.result:
            self.app.store.delete(self.index)
            self.app.show_home()


# ---- Main window ---------------------------------------------------------


class FPSBoosterApp(ctk.CTk):
    def __init__(self, store: PresetStore | None = None):
        ctk.set_appearance_mode("dark")
        super().__init__(fg_color=BG)
        self.store = store or PresetStore()
        self.title("FPS Booster")
        self.geometry("1080x700")
        self.minsize(960, 600)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=28, pady=(22, 14))
        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(side="left")
        ctk.CTkLabel(title, text="⚡", font=font(30, "bold"), text_color=ACCENT).pack(side="left")
        ctk.CTkLabel(title, text=" FPS", font=font(28, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkLabel(title, text="BOOSTER", font=font(28, "bold"), text_color=ACCENT).pack(
            side="left", padx=(8, 0)
        )

        ram = ctk.CTkFrame(header, fg_color=PANEL, corner_radius=12)
        ram.pack(side="right")
        self.ram_label = ctk.CTkLabel(ram, text="RAM", font=font(12, "bold"), text_color=TEXT)
        self.ram_label.pack(anchor="w", padx=14, pady=(8, 2))
        self.ram_bar = ctk.CTkProgressBar(ram, width=220, height=10, fg_color=PANEL_HI)
        self.ram_bar.pack(padx=14, pady=(0, 10))

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=22, pady=(0, 20))
        self.view: ctk.CTkFrame | None = None
        # lowercase process name -> CTkImage. Kept for the whole session so a
        # ticked program keeps its real icon after it has been closed.
        self._icons: dict[str, ctk.CTkImage] = {}

        self._update_ram()
        self.show_home()

    def _swap(self, view: ctk.CTkFrame):
        if self.view is not None:
            self.view.destroy()
        self.view = view
        view.pack(fill="both", expand=True)

    def show_home(self):
        self._swap(HomeView(self))

    def show_editor(self, index: int):
        self._swap(EditorView(self, index))

    def set_icon(self, name: str, image) -> None:
        current = self._icons.get(name.lower())
        if current is None or current.cget("light_image") is not image:
            self._icons[name.lower()] = ctk.CTkImage(image, image, size=(ICON_SIZE, ICON_SIZE))

    def icon_for(self, name: str) -> ctk.CTkImage:
        if name.lower() not in self._icons:
            self.set_icon(name, get_icon(name))  # letter badge
        return self._icons[name.lower()]

    def _update_ram(self):
        mem = psutil.virtual_memory()
        used_gb = (mem.total - mem.available) / 1024**3
        self.ram_label.configure(
            text=f"RAM  {mem.percent:.0f}%   ·   {used_gb:.1f} / {mem.total / 1024**3:.1f} GB"
        )
        self.ram_bar.set(mem.percent / 100)
        self.ram_bar.configure(
            progress_color=DANGER if mem.percent > 85 else WARN if mem.percent > 65 else ACCENT
        )
        self.after(1500, self._update_ram)


def main() -> None:
    FPSBoosterApp().mainloop()
