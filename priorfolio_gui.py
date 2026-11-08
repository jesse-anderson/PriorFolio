#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Jesse Anderson
"""Desktop front end. Run: priorfolio --gui or python -m priorfolio_gui."""
import json
import os
import queue
import threading
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import priorfolio as sa
import priorfolio_exports as exports

CHECKED, UNCHECKED = "☑", "☐"
HERE = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".priorfolio" / "settings.json"
TIP_DELAY_MS = 450
# How each kind of source is labelled in the list at the top of the window.
KIND_LABEL = {"folders": "Folder of projects", "projects": "Single project"}

COLUMN_HELP = {
    "sel": "Ticked projects are included when you press Generate. Click a row to tick or untick it.",
    "name": "Folder name of the project. Hover a row for its full path.",
    "commit": "First 10 characters of the latest git commit hash. A dash means there is no "
              "commit, so only the content hash can identify the project.",
    "date": "Date of that latest commit, as recorded by git on this computer.",
    "state": "How the files on disk compare with git and with the remote. Hover a row for "
             "what its state means.",
}

HELP_TEXT = """\
PriorFolio - IP disclosures and private evidence

1. Add a folder of projects or use Add single project.
2. Select a row and use Project details to describe the work, your contribution,
   its dates, and any references. Click or press Space to tick projects.
3. Leave Zip snapshots enabled to preserve the exact bytes. Generate the run.
4. Review the Disclosure tab. disclosure.zip contains only the disclosure text
   and JSON: no automatic local paths, remote URLs, or source-file listings.
   Titles and descriptions are your text; check them before sharing.
5. Use Export backup to save a private ZIP, including source snapshots. If the
   run holds likely secrets (.env, keys, credentials), export a FULL copy to
   keep offline and a CLOUD copy without them. Both share one timestamped record.
6. Put the copy you send in a password-protected 7-Zip archive (AES-256,
   Encrypt file names). Email it to yourself using Backup email, or save it
   to a cloud-sync folder.
7. Check the received email attachment or the completed upload in your cloud
   provider's web interface, ideally from another device. Confirm remote backup
   records YOUR check. A local export or a queued sync is not a remote backup.

PRIVATE BACKUP
The ZIP is not encrypted; the 7-Zip password protects it. It contains source
code, project paths, remote URLs and file names. Keep the password in a
password manager: without it the backup cannot be opened. Its .sha256 sidecar
checks the exported ZIP. A cloud copy verifies every file except those listed
in WITHHELD.txt; withholding is by file name, so check for secrets written
inside ordinary files. An email containing only a hash does not back up the
underlying files.

DISCLOSURE AND EVIDENCE
Content hash: SHA-256 of the file-hash manifest, sorted by relative path.
Bundle hash: SHA-256 of evidence.json, tying together the private evidence.
Keep that bundle hash separately, for example in your email.

evidence_bundle.zip is a smaller PRIVATE metadata bundle, without snapshots.
To verify that bundle after unpacking, select Metadata only. Full verification
requires the snapshots. Compare local projects reads paths recorded in the
bundle: use it only on evidence you created or trust. It reports changed and
missing files, not files added after the run.

TIMESTAMPS
Request timestamps (on by default) sends the SHA-256 of evidence.json to
DigiCert, Sectigo and FreeTSA. Like any web request, this reveals your IP
address to them; no project names or files are sent. Each signed response is
checked against that authority's root certificate, built into PriorFolio,
using OpenSSL 3 (included with Git for Windows). Without OpenSSL the
responses are kept but marked "not checked". Untick it to work offline.

PROJECT STATES
NOT A GIT REPO: a plain folder; it can still be hashed and snapshotted.
NO COMMITS: a repository without a commit yet.
Uncommitted: the working files differ from the committed version.
Repo config can run programs: the repository's own Git settings name a
program (such as a clean filter). PriorFolio skips git status there so it
never starts it; files are still hashed, but changes are not counted.
Remote state reflects local Git information; PriorFolio does not fetch.

Read-only scanning never commits, stages, fetches or changes your projects.
Warnings identify content that could not be read. Review and resolve them.
A hash identifies bytes; it does not establish authorship or ownership.
PriorFolio is not legal advice.

PriorFolio 0.1.0a2 - Copyright (c) 2026 Jesse Anderson
GNU GPL version 3. See LICENSE. Provided without warranty.
"""


def explain(p):
    """Plain-language description of one project's state, for the row tooltip."""
    lines = [p["path"], ""]
    if p["state"] == "FOLDER NOT FOUND":
        lines.append("This folder no longer exists. Remove it from Sources or rescan.")
        return "\n".join(lines)
    if p.get("git_error"):
        lines.append(f"Git could not read this repository: {p['git_error']}. No commit "
                     "hash can be recorded. Its files will still be hashed, as a plain "
                     "folder, and the run will carry a warning.")
        return "\n".join(lines)
    if not p["is_repo"]:
        lines.append("Not a git repository. There is no commit hash or commit date; "
                     "the content hash is the only thing that identifies it.")
        return "\n".join(lines)
    if p.get("config_warning"):
        lines.append(f"This repository's own Git settings name programs Git could run "
                     f"({p['config_warning']}). PriorFolio did not run git status here, so it "
                     "never starts them. Its files are still hashed, but uncommitted changes "
                     "are not counted. Check those settings if you did not add them.")
    if not p["commit"]:
        lines.append("Git repository with nothing committed yet. There is no commit "
                     "hash; the content hash is the only thing that identifies it.")
    if p["modified"] or p["untracked"]:
        lines.append(f"Since the last commit: {p['modified']} tracked file(s) changed, "
                     f"{p['untracked']} new file(s) or folder(s) added. Only the content "
                     "hash covers these changes.")
    if not p["remote"]:
        lines.append("No remote is configured. Copies may exist elsewhere.")
    elif p["unpushed"]:
        lines.append(f"{p['unpushed']} commit(s) have not been pushed to the remote.")
    if p["state"] == "clean, pushed":
        lines.append("The working tree is clean and local remote-tracking references "
                     "contain the commits. The remote server was not contacted.")
    if p["remote"]:
        lines += ["", f"Remote: {p['remote']}"]
    return "\n".join(lines)


class Tip:
    """One shared hover box. Call show(text) to schedule it and hide() to cancel."""

    def __init__(self, master):
        self.master = master
        self.window = None
        self.job = None

    def show(self, text):
        self.hide()
        self.job = self.master.after(TIP_DELAY_MS, lambda: self.popup(text))

    def popup(self, text):
        self.job = None
        x, y = self.master.winfo_pointerxy()
        self.window = tk.Toplevel(self.master)
        self.window.wm_overrideredirect(True)
        self.window.wm_attributes("-topmost", True)
        # Keep the box on screen when the pointer is near the right or bottom edge.
        x = min(x + 14, self.master.winfo_screenwidth() - 490)
        y = y + 18 if y < self.master.winfo_screenheight() - 200 else y - 150
        self.window.wm_geometry(f"+{max(x, 0)}+{y}")
        tk.Label(self.window, text=text, justify="left", wraplength=460,
                 background="#fffbe6", foreground="#1f1f1f", relief="solid",
                 borderwidth=1, padx=8, pady=6).pack()

    def hide(self):
        if self.job:
            self.master.after_cancel(self.job)
            self.job = None
        if self.window:
            self.window.destroy()
            self.window = None

    def attach(self, widget, text):
        widget.bind("<Enter>", lambda e: self.show(text), add="+")
        widget.bind("<Leave>", lambda e: self.hide(), add="+")
        widget.bind("<ButtonPress>", lambda e: self.hide(), add="+")


class FlowRow(ttk.Frame):
    """Wrap controls at their requested widths, including larger system fonts."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.controls = []
        self.rows = []
        self.groups = None
        self.layout_job = None
        self.bind("<Configure>", self.schedule_layout)

    def add(self, widget):
        self.controls.append(widget)
        widget.bind("<Configure>", self.schedule_layout, add="+")
        self.schedule_layout()

    def schedule_layout(self, event=None):
        if self.layout_job is None:
            self.layout_job = self.after_idle(self.layout)

    def layout(self):
        self.layout_job = None
        width = self.winfo_width()
        if width <= 1:
            # Wait for real geometry. One control per row during startup makes
            # the paned window cache an oversized lower pane and hide the list.
            return
        groups, row, used = [], [], 0
        for control in self.controls:
            required = control.winfo_reqwidth() + 8
            if row and used + required > width:
                groups.append(row)
                row, used = [], 0
            row.append(control)
            used += required
        if row:
            groups.append(row)
        if groups == self.groups:
            return
        self.groups = groups
        for control in self.controls:
            control.pack_forget()
        while len(self.rows) < len(groups):
            row_frame = ttk.Frame(self)
            row_frame.pack(fill="x", pady=(0, 3))
            self.rows.append(row_frame)
        for extra in self.rows[len(groups):]:
            extra.destroy()
        self.rows = self.rows[:len(groups)]
        for row_frame, controls in zip(self.rows, groups):
            for control in controls:
                control.pack(in_=row_frame, side="left", padx=(0, 8))
                # The layout frames are siblings created after the controls.
                # Keep them behind the controls so they do not intercept clicks.
                control.lift()

    def destroy(self):
        if self.layout_job is not None:
            self.after_cancel(self.layout_job)
            self.layout_job = None
        super().destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PriorFolio")
        # Fit the screen: a fixed size pushes the right-hand buttons off small displays.
        width = min(1150, self.winfo_screenwidth() - 60)
        height = min(840, self.winfo_screenheight() - 110)
        x = (self.winfo_screenwidth() - width) // 2
        self.geometry(f"{width}x{height}+{x}+10")
        self.minsize(min(820, width), min(560, height))

        self.projects = []
        self.checked = set()
        self.events = queue.Queue()
        self.out_dir = None
        self.busy = False
        self.tip = Tip(self)
        self.hovered = None
        self.help_window = None
        self.copies_window = None
        self.pane_job = None

        self.out_base = Path.home() / "PriorFolio" / "runs"
        self.snapshot_var = tk.BooleanVar(value=True)
        self.timestamp_var = tk.BooleanVar(value=True)
        self.metadata_var = tk.BooleanVar(value=False)
        self.compare_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="")
        self.backup_var = tk.StringVar(value="Generate a run, then export and confirm a remote backup.")
        self.project_details = self.load_details()
        self.runs = self.load_runs()

        self.sources = []  # (kind, path) pairs, in the same order as the listbox
        self.build()
        self.bind("<Configure>", self.schedule_panes, add="+")
        for kind, path in self.load_sources():
            self.add_source(kind, path)
        self.restore_last_run()
        self.poll_job = self.after(100, self.poll)
        self.scan()
        self.protocol("WM_DELETE_WINDOW", self.close)

    # ---- settings ----

    def load_sources(self):
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            sources = []
            for kind in KIND_LABEL:
                values = saved.get(kind, [])
                if isinstance(values, list):
                    sources.extend((kind, path) for path in values
                                   if isinstance(path, str) and path and Path(path).is_dir())
        except (OSError, ValueError, AttributeError, TypeError):
            sources = []
        return sources

    def load_details(self):
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8")).get("details", {})
            return {key: {field: text for field, text in value.items()
                          if field in exports.DETAIL_FIELDS and isinstance(text, str)}
                    for key, value in saved.items() if isinstance(value, dict)}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def save_sources(self):
        data = {kind: [p for k, p in self.sources if k == kind] for kind in KIND_LABEL}
        data["details"] = self.project_details
        data["runs"] = [str(path) for path in self.runs]
        if self.out_dir:
            data["last_run"] = str(self.out_dir)
        temporary = None
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=SETTINGS.parent,
                                             prefix=".priorfolio-settings-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=2)
            os.replace(temporary, SETTINGS)
        except OSError:
            self.status_var.set("Settings could not be saved. This session still works.")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def restore_last_run(self):
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            self.activate_run(Path(saved["last_run"]))
        except (OSError, ValueError, KeyError, TypeError, sa.ToolError):
            self.update_pending_message()

    def load_runs(self):
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            values = saved.get("runs", [])
            if not isinstance(values, list):
                values = []
            values = [saved.get("last_run")] + values
            return list(dict.fromkeys(Path(value).resolve() for value in values if isinstance(value, str) and value))
        except (OSError, ValueError, AttributeError, TypeError):
            return []

    @staticmethod
    def set_text(widget, content):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def activate_run(self, folder):
        """Switch the folder, text and backup state together, without stale text."""
        folder = Path(folder).resolve()
        if sa.run_summary(folder) is None:
            raise sa.ToolError("This folder has no readable PriorFolio evidence record.")
        documents = []
        for widget, filename in ((self.table_text, "disclosure.md"), (self.email_text, "email.txt")):
            try:
                content = sa.safe_child(folder, filename).read_text(encoding="utf-8")
            except FileNotFoundError:
                content = f"{filename} is not included in this run."
            documents.append((widget, content))
        self.out_dir = folder
        self.runs = [folder] + [path for path in self.runs if path != folder]
        for widget, content in documents:
            self.set_text(widget, content)
        self.open_btn.configure(state="normal")
        self.refresh_backup()

    def pending_runs(self):
        # A deleted run cannot be backed up any more, so it must not block closing.
        # A run on a disconnected drive is remembered and counts again once it returns.
        return [path for path in self.runs if path.is_dir()
                and exports.backup_status(path).get("state") != "confirmed_by_user"]

    def update_pending_message(self):
        pending = self.pending_runs()
        if pending:
            self.backup_var.set(f"{len(pending)} saved run(s) still need a remote backup. Open Previous runs to review them.")

    def add_source(self, kind, path):
        self.sources.append((kind, path))
        self.folders.insert("end", f"{KIND_LABEL[kind]}:  {path}")

    # ---- layout ----

    def build(self):
        tip = self.tip.attach

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Sources").pack(side="left", anchor="n")
        self.folders = tk.Listbox(top, height=4, selectmode="extended", activestyle="none")
        self.folders.pack(side="left", fill="x", expand=True, padx=6)
        tip(self.folders, "Where to look. A 'Folder of projects' is scanned and each "
                          "sub-folder becomes one row below. A 'Single project' is listed "
                          "as one row.")
        buttons = ttk.Frame(top)
        buttons.pack(side="left", anchor="n")
        add = ttk.Button(buttons, text="Add folder", command=lambda: self.pick("folders"))
        add.grid(row=0, column=0, sticky="ew")
        tip(add, "Add a folder that holds many projects. Each sub-folder becomes one row. "
                 "You can add as many folders as you need.")
        single = ttk.Button(buttons, text="Add single project", command=lambda: self.pick("projects"))
        single.grid(row=1, column=0, sticky="ew", pady=2)
        tip(single, "Add one folder as one project, whether or not it is a git repo. Use "
                    "this for a project that lives on its own, or one nested deeper than "
                    "the folder scan looks.")
        remove = ttk.Button(buttons, text="Remove", command=self.remove_source)
        remove.grid(row=2, column=0, sticky="ew")
        tip(remove, "Remove the highlighted entries from the list. Nothing on disk is deleted.")
        self.scan_btn = ttk.Button(buttons, text="Rescan", command=self.scan)
        self.scan_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        tip(self.scan_btn, "Re-read every folder. Use this after committing or pushing, so the "
                           "list shows the current state. Clears your ticks.")
        info = ttk.Button(buttons, text="ⓘ How it works", command=self.show_help)
        info.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=2)
        tip(info, "Open a plain-language guide: the steps, what every column, hash and "
                  "option means, and what this tool does not prove.")

        panes = self.panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8)

        upper = ttk.Frame(panes)
        panes.add(upper, weight=3)
        self.tree = ttk.Treeview(upper, columns=tuple(COLUMN_HELP), show="headings", selectmode="browse")
        for col, text, width, stretch in (
            ("sel", "", 34, False), ("name", "Project", 320, True),
            ("commit", "Commit", 100, False), ("date", "Commit date", 100, False),
            ("state", "State", 420, True),
        ):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        background = ttk.Style(self).lookup("Treeview", "background") or "white"
        dark = sum(self.winfo_rgb(background)) < 3 * 32768
        self.tree.tag_configure("nohash", foreground="#ff9090" if dark else "#b00020")
        self.tree.tag_configure("dirty", foreground="#ffd580" if dark else "#805500")
        scroll = ttk.Scrollbar(upper, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Motion>", self.on_hover)
        self.tree.bind("<Leave>", self.on_leave)
        self.tree.bind("<space>", self.toggle_focused)

        lower = ttk.Frame(panes)
        panes.add(lower, weight=2)

        bar = FlowRow(lower, padding=(0, 8))
        bar.pack(fill="x")
        for text, width, pad, predicate, help_text in (
            ("All", 6, 0, lambda p: True, "Tick every project."),
            ("None", 6, 4, lambda p: False, "Untick every project."),
            ("With a remote", None, 0, lambda p: bool(p["remote"]),
             "Tick projects with a configured remote. This does not contact the remote or prove a backup exists."),
        ):
            b = ttk.Button(bar, text=text, command=lambda f=predicate: self.check(f))
            if width:
                b.configure(width=width)
            bar.add(b)
            tip(b, help_text)

        details = ttk.Button(bar, text="Project details", command=self.edit_details)
        bar.add(details)
        tip(details, "Edit the focused project's public title, description, contribution, dates and references.")

        snap = ttk.Checkbutton(bar, text="Zip snapshots", variable=self.snapshot_var)
        bar.add(snap)
        tip(snap, "Also save a zip of the exact files hashed, one per project. Keep these: a "
                  "hash is only useful if you can later produce the files that match it. "
                  "Export backup includes these snapshots in the private ZIP for email or cloud storage.")
        stamp = ttk.Checkbutton(bar, text="Request timestamps (sends a hash; reveals your IP)",
                                variable=self.timestamp_var)
        bar.add(stamp)
        tip(stamp, "Sends the SHA-256 of evidence.json to DigiCert, Sectigo and FreeTSA, which return "
                   "signed proof of when that hash existed. Like any web request, this reveals your IP "
                   "address to them. No project names or files are sent. PriorFolio checks each "
                   "signature against the authority's root certificate. Untick to work offline.")

        bar = FlowRow(lower, padding=(0, 0, 0, 8))
        bar.pack(fill="x")

        self.open_btn = ttk.Button(bar, text="Open output folder", command=self.open_output, state="disabled")
        tip(self.open_btn, "Open the latest run. Review disclosure.zip before sharing; use Export backup for the complete private archive.")
        self.run_btn = ttk.Button(bar, text="Generate", command=self.generate)
        tip(self.run_btn, "Hash the ticked projects and write a new dated output folder. "
                          "Your projects are only read, never changed.")
        self.verify_btn = ttk.Button(bar, text="Verify a run", command=self.verify)
        tip(self.verify_btn, "Re-check an earlier output folder: is the evidence intact, and "
                             "which project files have changed on disk since then?")
        self.backup_btn = ttk.Button(bar, text="Export backup", command=self.export_backup, state="disabled")
        tip(self.backup_btn, "Save a PRIVATE ZIP, including source snapshots. If the run holds likely secrets, "
                             "save a full copy to keep offline and a cloud copy without them. "
                             "7-Zip it with a password before it leaves this computer.")
        self.confirm_btn = ttk.Button(bar, text="Confirm remote backup", command=self.confirm_backup, state="disabled")
        tip(self.confirm_btn, "Record your check that the email attachment arrived or the cloud upload completed.")
        self.history_btn = ttk.Button(bar, text="Previous runs", command=self.show_history)
        tip(self.history_btn, "Open a saved run and review its disclosure and remote backup status, including older pending backups.")
        for control in (self.run_btn, self.verify_btn, self.open_btn, self.backup_btn, self.confirm_btn, self.history_btn):
            bar.add(control)

        checks = ttk.Frame(lower)
        checks.pack(fill="x")
        for label, variable, help_text in (
            ("Metadata only", self.metadata_var, "Verify an unpacked evidence_bundle.zip without source snapshots; does not certify a complete backup."),
            ("Compare local projects", self.compare_var, "Read project paths from the selected run. Enable only for a run you trust."),
        ):
            control = ttk.Checkbutton(checks, text=label, variable=variable)
            control.pack(side="left", padx=(0, 12))
            tip(control, help_text)
        ttk.Label(lower, text="Disclosure: review before sharing. Private backup: includes paths, source code and possible credentials.",
                  wraplength=760).pack(fill="x", pady=4)

        self.tabs = ttk.Notebook(lower)
        self.tabs.pack(fill="both", expand=True)
        self.email_text = self.add_tab(
            "Backup email",
            "Copy the email, attach the complete exported backup ZIP, and send it to yourself.")
        self.table_text = self.add_tab(
            "Disclosure",
            "Copy the disclosure after reviewing your descriptions and contributions. It omits automatic local paths and file listings.")
        self.tabs.select(self.table_text.master)

        bottom = ttk.Frame(self, padding=8)
        # Reserve room for backup status before the resizable panes take space.
        bottom.pack(side="bottom", fill="x", before=panes)
        ttk.Label(bottom, textvariable=self.backup_var, wraplength=760).pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var, wraplength=760).pack(fill="x")

    def schedule_panes(self, event=None):
        if self.pane_job is None:
            self.pane_job = self.after_idle(self.fit_panes)

    def fit_panes(self):
        """Keep both project selection and the preview usable after reflow."""
        self.pane_job = None
        upper, lower = self.panes.winfo_children()
        available = self.panes.winfo_height()
        if available <= 1:
            return
        # Native pane weights alone can give all spare space to either pane.
        # Keep the user's divider position unless controls or text are clipped.
        sash = max(1, lower.winfo_y() - upper.winfo_height())
        text_request = max(self.email_text.winfo_reqheight(), self.table_text.winfo_reqheight())
        minimum_lower = lower.winfo_reqheight() - text_request + 72
        minimum_upper = 96
        maximum_upper = max(minimum_upper, available - sash - minimum_lower)
        current = self.panes.sashpos(0)
        target = max(minimum_upper, min(current, maximum_upper))
        if current != target:
            self.panes.sashpos(0, target)

    def add_tab(self, title, copy_help):
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=title)
        text = tk.Text(frame, wrap="word", height=10, font="TkFixedFont")
        text.configure(state="disabled")
        ys = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=ys.set)
        copy = ttk.Button(frame, text="Copy", command=lambda: self.copy(text))
        copy.pack(side="right", anchor="n", padx=4, pady=4)
        self.tip.attach(copy, copy_help)
        ys.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    def text_window(self, title, content):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry(f"{min(720, self.winfo_screenwidth() - 60)}x"
                     f"{min(680, self.winfo_screenheight() - 120)}")
        text = tk.Text(win, wrap="word", font="TkFixedFont", padx=14, pady=12)
        ys = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", content)
        text.configure(state="disabled")
        return win

    def show_help(self):
        if self.help_window and self.help_window.winfo_exists():
            self.help_window.lift()
            return
        self.help_window = self.text_window("How it works", HELP_TEXT)

    # ---- hover help for the project list ----

    def on_hover(self, event):
        region = self.tree.identify_region(event.x, event.y)
        column = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if region == "heading" and column:
            key = ("heading", column)
            text = COLUMN_HELP[self.tree.column(column, "id")]
        elif region == "cell" and row:
            key = ("row", row)
            text = explain(self.projects[int(row)])
        else:
            key, text = None, None
        if key != self.hovered:
            self.hovered = key
            self.tip.show(text) if text else self.tip.hide()

    def on_leave(self, event):
        self.hovered = None
        self.tip.hide()

    # ---- background work ----

    def work(self, fn, done):
        def runner():
            try:
                self.events.put(("done", done, fn()))
            except Exception as e:
                self.events.put(("error", None, str(e)))

        self.set_busy(True)
        threading.Thread(target=runner, daemon=True).start()

    def log(self, message):
        self.events.put(("log", None, message))

    def poll(self):
        try:
            while True:
                kind, done, payload = self.events.get_nowait()
                if kind == "log":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.set_busy(False)
                    self.status_var.set("Failed.")
                    messagebox.showerror("PriorFolio", payload)
                else:
                    self.set_busy(False)
                    try:
                        done(payload)
                    except Exception as e:
                        self.status_var.set("Could not display the result.")
                        messagebox.showerror("PriorFolio", str(e))
        except queue.Empty:
            pass
        self.poll_job = self.after(100, self.poll)

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.run_btn.configure(state=state)
        self.scan_btn.configure(state=state)
        self.verify_btn.configure(state=state)
        self.history_btn.configure(state=state)
        self.backup_btn.configure(state="disabled" if busy or not self.out_dir else "normal")
        ready = self.out_dir and exports.backup_status(self.out_dir).get("state") in ("exported", "confirmed_by_user")
        self.confirm_btn.configure(state="normal" if ready and not busy else "disabled")

    # ---- actions ----

    def pick(self, kind):
        if self.busy:
            return
        title = ("Choose a folder that holds projects" if kind == "folders"
                 else "Choose the folder of one project")
        chosen = filedialog.askdirectory(
            title=title,
            initialdir=self.sources[-1][1] if self.sources else str(Path.home()))
        if not chosen:
            return
        chosen = str(Path(chosen).resolve())
        if (kind, chosen) in self.sources:
            self.status_var.set("That entry is already in the list.")
            return
        self.add_source(kind, chosen)
        self.save_sources()
        self.scan()

    def remove_source(self):
        if self.busy:
            return
        picked = self.folders.curselection()
        if not picked:
            messagebox.showinfo("PriorFolio", "Highlight an entry in the Sources list first.")
            return
        for index in reversed(picked):
            self.folders.delete(index)
            del self.sources[index]
        self.save_sources()
        self.scan()

    def scan(self):
        if self.busy:
            return
        roots = [Path(p) for k, p in self.sources if k == "folders" and Path(p).is_dir()]
        singles = [Path(p) for k, p in self.sources if k == "projects" and Path(p).is_dir()]
        if not roots and not singles:
            self.show_projects([])
            return
        self.work(lambda: sa.scan_roots(roots, singles, log=self.log), self.show_projects)

    def show_projects(self, projects):
        self.projects = projects
        self.checked.clear()
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(projects):
            p["disclosure"] = dict(self.project_details.get(p["path"], {}))
            if not p["commit"]:
                tags = ("nohash",)
            elif p["state"] != "clean, pushed":
                tags = ("dirty",)
            else:
                tags = ()
            self.tree.insert("", "end", iid=str(i), tags=tags, values=(
                UNCHECKED, p["name"], (p["commit"] or "-")[:10],
                (p["commit_date"] or "-")[:10], p["state"]))
        if projects:
            self.status_var.set(f"{len(projects)} projects found. Click rows to tick them. "
                                "Hover anything for an explanation.")
        else:
            self.status_var.set("No projects. Use Add folder or Add single project.")

    def on_click(self, event):
        self.tip.hide()
        row = self.tree.identify_row(event.y)
        # Native themes do not agree on the region label for a data cell.
        # A row and a displayed column identify the clickable area directly.
        if row and not self.busy and self.tree.identify_column(event.x):
            self.tree.focus(row)
            self.tree.focus_set()
            self.set_checked(int(row), int(row) not in self.checked)
            self.show_count()

    def toggle_focused(self, event=None):
        row = self.tree.focus()
        if row and not self.busy:
            self.set_checked(int(row), int(row) not in self.checked)
            self.show_count()
        return "break"

    def edit_details(self):
        row = self.tree.focus()
        if self.busy:
            return
        if not row:
            messagebox.showinfo("Project details", "Focus a project row first.")
            return
        project = self.projects[int(row)]
        win = tk.Toplevel(self)
        win.title("Disclosure details: " + project["name"])
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        fields = {}
        for field, label in (("title", "Public title"), ("description", "Description"),
                             ("contribution", "My contribution"), ("period", "Work dates or period"),
                             ("references", "References (shared exactly as entered)")):
            ttk.Label(frame, text=label).pack(anchor="w")
            widget = tk.Text(frame, height=1 if field in ("title", "period") else 3,
                             width=65, wrap="word", font="TkTextFont")
            widget.insert("1.0", project["disclosure"].get(field, project["name"] if field == "title" else ""))
            widget.pack(fill="x", pady=(2, 8))
            fields[field] = widget

        def save():
            details = {key: widget.get("1.0", "end-1c").strip() for key, widget in fields.items()}
            project["disclosure"] = details
            self.project_details[project["path"]] = details
            self.save_sources()
            win.destroy()

        ttk.Button(frame, text="Save details", command=save).pack(anchor="e")
        fields["title"].focus_set()

    def set_checked(self, index, on):
        (self.checked.add if on else self.checked.discard)(index)
        self.tree.set(str(index), "sel", CHECKED if on else UNCHECKED)

    def check(self, predicate):
        if self.busy:
            return
        for i, p in enumerate(self.projects):
            self.set_checked(i, predicate(p))
        self.show_count()

    def show_count(self):
        self.status_var.set(f"{len(self.checked)} of {len(self.projects)} selected.")

    def generate(self):
        if self.busy:
            return
        chosen = [self.projects[i] for i in sorted(self.checked)]
        if not chosen:
            messagebox.showinfo("PriorFolio", "Tick at least one project first.")
            return
        # Tk variables can only be read on the main thread, so read them before the worker starts.
        snapshot, timestamp, out_base = self.snapshot_var.get(), self.timestamp_var.get(), self.out_base
        self.work(lambda: sa.generate(chosen, out_base, snapshot, timestamp, log=self.log),
                  self.show_run)

    def show_run(self, run):
        self.activate_run(run["out_dir"])
        failed = [t["tsa"] for t in run["timestamps"] if not t["ok"]]
        verified = [t["tsa"] for t in run["timestamps"] if t["ok"] and t.get("signature_verified")]
        unchecked = [t["tsa"] for t in run["timestamps"] if t["ok"] and not t.get("signature_verified")]
        status = (f"Generated {len(run['results'])} projects and verified the saved run. "
                  f"Bundle SHA-256 {run['bundle']}.")
        if verified:
            status += f" Signed timestamps verified: {', '.join(verified)}."
        if unchecked:
            status += f" Timestamps NOT checked (OpenSSL 3 not found): {', '.join(unchecked)}."
        if failed:
            status += f" Timestamp FAILED: {', '.join(failed)}."
        if run["warnings"]:
            status += f" {len(run['warnings'])} WARNING(S)."
        self.status_var.set(status)
        self.refresh_backup()
        self.save_sources()
        if run["warnings"]:
            shown = run["warnings"][:15]
            more = len(run["warnings"]) - len(shown)
            messagebox.showwarning(
                "PriorFolio",
                "Some content may be missing from the hashes:\n\n" + "\n".join(shown)
                + (f"\n... and {more} more (see evidence.json)" if more else "")
                + "\n\nFix the cause and generate again so the hash covers everything.")

    def verify(self):
        if self.busy:
            return
        start = self.out_dir.parent if self.out_dir else self.out_base
        folder = filedialog.askdirectory(
            title="Choose a run folder to verify",
            initialdir=str(start if Path(start).is_dir() else Path.home()))
        if not folder:
            return
        metadata_only, compare = self.metadata_var.get(), self.compare_var.get()

        def job():
            summary = sa.run_summary(folder, signatures=True)
            evidence = sa.verify_run(folder, metadata_only=metadata_only)
            changed = []
            if summary and compare:
                changed = [m for m in sa.verify_run(folder, against_disk=True, metadata_only=metadata_only) if m not in evidence]
            return folder, summary, evidence, changed, metadata_only, compare

        self.status_var.set("Verifying ...")
        self.work(job, self.show_verify)

    def show_verify(self, outcome):
        folder, summary, evidence, changed, metadata_only, compare = outcome
        lines = [f"Run folder: {folder}", ""]
        if summary:
            lines += [f"Bundle SHA-256: {summary['bundle']}",
                      f"Generated (UTC): {summary['generated_utc']}",
                      f"Projects: {summary['projects']}"]
            lines += [f"Timestamp {name}: {when or 'time not readable'} ({sa.SIGNATURE_LABELS[state]})"
                      for name, when, state in summary["timestamps"]]
            lines.append("")
        if evidence:
            lines += [f"EVIDENCE: {len(evidence)} PROBLEM(S)"] + ["  " + m for m in evidence]
        else:
            lines.append("METADATA: OK. Source snapshots were NOT checked." if metadata_only else
                         "EVIDENCE: OK. Evidence hashes match.")
        try:
            withheld = exports.withheld_files(folder)
        except (OSError, ValueError):
            withheld = []  # reported above as an evidence problem
        if withheld:
            lines += ["", f"CLOUD COPY: {len(withheld)} file(s) were left out of its snapshots "
                          "(see WITHHELD.txt). Their hashes are still recorded, but their contents "
                          "can only be checked with the full copy:"]
            lines += [f"  {f['project']}: {f['path']}" for f in withheld]
        if summary:
            lines.append("")
            if not compare:
                lines.append("Local project folders were not read.")
            elif changed:
                lines += [f"PROJECTS ON DISK: {len(changed)} difference(s) since this run"]
                lines += ["  " + m for m in changed]
                lines.append("  (Expected if you kept working. The run itself is unaffected.)")
            else:
                lines.append("PROJECTS ON DISK: every hashed file is still identical.")
            lines += ["", "Compare the bundle hash above with the one in your email or signed",
                      "agreement. That outside copy is what shows this folder was not rewritten."]
        self.verify_report = "\n".join(lines)
        self.text_window("Verify a run", self.verify_report)
        if summary and not evidence:
            self.activate_run(folder)
            self.save_sources()
        self.status_var.set(("Verify: metadata OK; source snapshots not checked." if metadata_only else "Verify: evidence OK.") if not evidence
                            else f"Verify: {len(evidence)} problem(s) found.")

    def copy(self, widget):
        self.clipboard_clear()
        self.clipboard_append(widget.get("1.0", "end-1c"))
        self.status_var.set("Copied to clipboard.")

    def open_output(self):
        if self.out_dir:
            if sys.platform == "win32":
                os.startfile(self.out_dir)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.out_dir)])

    def refresh_backup(self):
        status = exports.backup_status(self.out_dir)
        state = status.get("state")
        withheld = len(status.get("withheld") or [])
        if withheld:
            full = ("the full copy was exported for offline storage" if status.get("full_copy")
                    else "export a FULL copy too and keep it offline")
            copy = f" It is a cloud copy without {withheld} file(s); {full}."
        else:
            copy = ""
        if state == "confirmed_by_user":
            message = f"Remote backup confirmed by you ({status.get('method')}, {status.get('confirmed_utc')}).{copy}"
        elif state == "exported":
            message = ("Backup ZIP exported. 7-Zip it with a password, email or upload it, check the "
                       f"remote copy, then confirm remote backup.{copy}")
        else:
            message = "BACKUP REQUIRED: export the complete ZIP, then email it to yourself or upload it to private cloud storage."
        others = [path for path in self.pending_runs() if path != self.out_dir]
        if others:
            message += f" {len(others)} other run(s) still need a backup; see Previous runs."
        self.backup_var.set(message)
        self.backup_btn.configure(state="normal")
        self.confirm_btn.configure(state="normal" if state in ("exported", "confirmed_by_user") else "disabled")

    def show_history(self):
        if self.busy:
            return
        if not self.runs:
            messagebox.showinfo("Previous runs", "No saved runs yet. Generate a run or use Verify a run to open one.")
            return
        win = tk.Toplevel(self)
        win.title("Previous runs")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Open a run to view its saved documents and finish its backup.").pack(anchor="w")
        rows = tk.Listbox(frame, width=85, height=min(12, len(self.runs)), exportselection=False)
        paths = list(self.runs)
        labels = {"backup_required": "BACKUP REQUIRED", "exported": "EXPORTED - remote check pending",
                  "confirmed_by_user": "Confirmed by you"}
        for path in paths:
            state = exports.backup_status(path).get("state", "backup_required")
            label = labels[state] if path.is_dir() else "FOLDER NOT FOUND"
            rows.insert("end", f"{path.name} | {label} | {path.parent}")
        rows.pack(fill="both", expand=True, pady=8)
        rows.selection_set(0)

        def open_selected():
            selected = rows.curselection()
            if not selected:
                return
            try:
                self.activate_run(paths[selected[0]])
                self.save_sources()
            except (OSError, ValueError, sa.ToolError) as e:
                messagebox.showerror("Previous runs", str(e))
                return
            self.status_var.set("Opened saved documents. Use Verify a run to check evidence integrity.")
            win.destroy()

        ttk.Button(frame, text="Open selected run", command=open_selected).pack(anchor="e")
        rows.bind("<Double-Button-1>", lambda event: open_selected())
        rows.bind("<Return>", lambda event: open_selected())

    def export_backup(self):
        if self.busy or not self.out_dir:
            return
        secrets = exports.likely_secrets(self.out_dir)
        if secrets:
            self.choose_copies(secrets)
        else:
            self.export_copies(None)

    def choose_copies(self, secrets):
        """Offer a full copy for offline storage and a cloud copy without likely secrets."""
        win = self.copies_window = tk.Toplevel(self)
        win.title("Backup copies")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=560, justify="left", text=(
            f"{len(secrets)} file(s) in this run have names that often hold passwords, tokens or "
            "private keys. PriorFolio can export two copies of the same timestamped record:\n\n"
            "  - a FULL copy with every file, to keep offline, such as on an external drive;\n"
            "  - a CLOUD copy that leaves out the files selected below, to email or upload.\n\n"
            "Selection is by file name only. Put each copy in a password-protected 7-Zip "
            "archive before it leaves this computer.")).pack(fill="x")
        rows = tk.Listbox(frame, selectmode="multiple", exportselection=False, width=80,
                          height=min(10, len(secrets)))
        for item in secrets:
            rows.insert("end", item)
        rows.selection_set(0, "end")
        rows.pack(fill="both", expand=True, pady=10)
        self.tip.attach(rows, "Selected files are left out of the cloud copy. Click to change the selection.")
        actions = ttk.Frame(frame)
        actions.pack(anchor="e")

        def finish(withhold):
            win.destroy()
            self.copies_window = None
            self.export_copies(withhold)

        ttk.Button(actions, text="Export full and cloud copies",
                   command=lambda: finish([secrets[i] for i in rows.curselection()] or None)).pack(side="left")
        ttk.Button(actions, text="Full copy only", command=lambda: finish(None)).pack(side="left", padx=6)
        ttk.Button(actions, text="Cancel", command=win.destroy).pack(side="left")

    def export_copies(self, withhold):
        """Ask where to save the full copy, and the cloud copy when files are withheld."""
        run_dir, name = self.out_dir, self.out_dir.name
        types = dict(defaultextension=".zip", filetypes=[("ZIP archive", "*.zip")])
        if withhold:
            full = filedialog.asksaveasfilename(
                title="Save the FULL private backup (keep it offline)", initialdir=str(Path.home()),
                initialfile=f"PriorFolio-{name}-full.zip", **types)
        else:
            full = filedialog.asksaveasfilename(
                title="Save PRIVATE backup for email or a cloud-sync folder", initialdir=str(Path.home()),
                initialfile=f"PriorFolio-{name}-private.zip", **types)
        if not full:
            return
        cloud = None
        if withhold:
            cloud = filedialog.asksaveasfilename(
                title=f"Save the CLOUD copy ({len(withhold)} file(s) withheld)",
                initialdir=str(Path(full).parent), initialfile=f"PriorFolio-{name}-cloud.zip", **types)

        def job():
            status = exports.export_backup(run_dir, full)
            if cloud:
                status = exports.export_backup(run_dir, cloud, withhold)
            return status

        self.work(job, self.show_backup)

    def show_backup(self, status):
        self.refresh_backup()
        if status.get("withheld"):
            self.status_var.set(f"Exported the full copy and a cloud copy without {len(status['withheld'])} "
                                f"file(s): {status['archive']}. SHA-256 {status['archive_sha256']}. "
                                "7-Zip it with a password before uploading.")
        else:
            self.status_var.set(f"Exported {status['archive']}. SHA-256 {status['archive_sha256']}. "
                                "7-Zip it with a password before it leaves this computer.")

    def confirm_backup(self):
        if self.busy or not self.out_dir:
            return
        win = tk.Toplevel(self)
        win.title("Confirm remote backup")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, wraplength=460, text="Check the received email attachment or the completed upload in your cloud provider's web interface. PriorFolio records your confirmation; it cannot inspect your account.").pack(fill="x")
        method = tk.StringVar(value="cloud")
        for label, value in (("Email to myself", "email"), ("Private cloud storage", "cloud")):
            ttk.Radiobutton(frame, text=label, value=value, variable=method).pack(anchor="w", pady=4)
        checked = tk.BooleanVar(value=False)
        confirm = ttk.Button(frame, text="Record confirmation", state="disabled")
        ttk.Checkbutton(frame, text="I checked that the complete ZIP is available remotely.", variable=checked,
                        command=lambda: confirm.configure(state="normal" if checked.get() else "disabled")).pack(pady=8)

        def save():
            try:
                exports.confirm_backup(self.out_dir, method.get())
            except (OSError, sa.ToolError) as e:
                messagebox.showerror("Backup confirmation", str(e), parent=win)
                return
            self.refresh_backup()
            win.destroy()

        confirm.configure(command=save)
        confirm.pack(anchor="e")

    def close(self):
        if self.busy:
            messagebox.showinfo("PriorFolio", "Wait for the current operation to finish before closing.")
            return
        pending = self.pending_runs()
        if pending:
            if not messagebox.askyesno("Remote backup pending",
                    f"{len(pending)} saved run(s) have no confirmed remote backup. Your local files are saved, but still need "
                    "an email-to-yourself or cloud copy. Close now and finish the backup later?"):
                return
        self.destroy()

    def destroy(self):
        if getattr(self, "pane_job", None):
            self.after_cancel(self.pane_job)
            self.pane_job = None
        if getattr(self, "poll_job", None):
            self.after_cancel(self.poll_job)
            self.poll_job = None
        if getattr(self, "tip", None):
            self.tip.hide()
        super().destroy()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
