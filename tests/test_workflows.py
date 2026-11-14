"""Exercise run navigation, recovery and failure paths through real Tk widgets."""
import gc
import hashlib
import json
import shutil
import tkinter as tk
import zipfile
from pathlib import Path

import pytest

import priorfolio_gui as gui
import priorfolio as core
import priorfolio_exports as exports
from conftest import write, git
from test_gui import button, click_row, settle


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    write(root / "Alpha", {"work.txt": "alpha\n"})
    write(root / "Beta", {"work.txt": "beta\n"})
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"folders": [str(root)]}))
    monkeypatch.setattr(gui, "SETTINGS", settings)
    dialogs = []
    for name in ("showerror", "showinfo", "showwarning"):
        monkeypatch.setattr(gui.messagebox, name, lambda title, text, **kw: dialogs.append(text))
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **kw: False)
    gc.collect()
    gc.disable()
    try:
        app = gui.App()
    except tk.TclError as exc:
        gc.enable()
        if "no display" in str(exc).lower() or "couldn't connect to display" in str(exc).lower():
            pytest.skip("no display")
        raise
    app.out_base = tmp_path / "runs"
    app.timestamp_default = app.timestamp_var.get()
    app.timestamp_var.set(False)  # on by default; tests stay offline
    app.dialogs = dialogs
    app.callback_errors = []
    app.report_callback_exception = lambda kind, value, traceback: app.callback_errors.append(value)
    settle(app)
    try:
        yield app
        assert not app.callback_errors, app.callback_errors
    finally:
        app.destroy()
        gc.enable()


def generate(app, name):
    button(app, "None").invoke()
    click_row(app, name)
    button(app, "Generate").invoke()
    settle(app)
    return app.out_dir


def test_verifying_a_different_run_switches_all_displayed_documents(desktop, monkeypatch):
    alpha = generate(desktop, "Alpha")
    generate(desktop, "Beta")
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(alpha))
    button(desktop, "Verify a run").invoke()
    settle(desktop)
    assert desktop.out_dir == alpha
    assert desktop.table_text.get("1.0", "end-1c") == (alpha / "disclosure.md").read_text(encoding="utf-8")
    assert desktop.email_text.get("1.0", "end-1c") == (alpha / "email.txt").read_text(encoding="utf-8")


def test_generated_disclosure_cannot_be_edited_without_changing_export(desktop):
    run = generate(desktop, "Alpha")
    desktop.table_text.insert("end", "UNSAVED EDIT")
    assert desktop.table_text.get("1.0", "end-1c") == (run / "disclosure.md").read_text(encoding="utf-8")
    desktop.copy(desktop.table_text)
    assert desktop.clipboard_get() == (run / "disclosure.md").read_text(encoding="utf-8")


def test_metadata_bundle_without_documents_does_not_show_previous_run(desktop, tmp_path, monkeypatch):
    alpha = generate(desktop, "Alpha")
    received = tmp_path / "metadata"
    with zipfile.ZipFile(alpha / "evidence_bundle.zip") as z:
        z.extractall(received)
    generate(desktop, "Beta")
    desktop.metadata_var.set(True)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(received))
    button(desktop, "Verify a run").invoke()
    settle(desktop)
    assert "Beta" not in desktop.table_text.get("1.0", "end")
    assert "Beta" not in desktop.email_text.get("1.0", "end")


def test_small_window_keeps_action_controls_and_output_visible(desktop):
    desktop.geometry("820x560")
    generate(desktop, "Alpha")
    desktop.update()
    for label in ("Project details", "Generate", "Verify a run", "Open output folder", "Export backup", "Confirm remote backup", "Previous runs"):
        widget = button(desktop, label)
        assert widget.winfo_viewable(), label
        assert widget.winfo_rootx() >= desktop.winfo_rootx(), label
        assert widget.winfo_rootx() + widget.winfo_width() <= desktop.winfo_rootx() + desktop.winfo_width(), label
        assert widget.winfo_rooty() + widget.winfo_height() <= desktop.winfo_rooty() + desktop.winfo_height(), label
    assert desktop.table_text.winfo_height() >= 60, {
        "window": desktop.winfo_geometry(),
        "panes": desktop.panes.winfo_geometry(),
        "divider": desktop.panes.sashpos(0),
        "pane_requests": [pane.winfo_reqheight() for pane in desktop.panes.winfo_children()],
        "preview": desktop.table_text.winfo_geometry(),
    }


def test_cancelled_export_changes_nothing(desktop, monkeypatch):
    run = generate(desktop, "Alpha")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: "")
    button(desktop, "Export backup").invoke()
    settle(desktop)
    assert exports.backup_status(run)["state"] == "backup_required"
    assert not (run / "backup_status.json").exists()


def test_failed_export_recovers_controls_and_leaves_no_receipt(desktop, tmp_path, monkeypatch):
    run = generate(desktop, "Alpha")
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(tmp_path / "absent" / "backup.zip"))
    button(desktop, "Export backup").invoke()
    settle(desktop)
    assert desktop.dialogs
    assert str(button(desktop, "Export backup").cget("state")) == "normal"
    assert str(button(desktop, "Generate").cget("state")) == "normal"
    assert exports.backup_status(run)["state"] == "backup_required"


def test_complete_backup_survives_local_receipt_write_failure(desktop, tmp_path, monkeypatch):
    run = generate(desktop, "Alpha")
    target = tmp_path / "complete.zip"
    def unavailable(*args):
        raise PermissionError("settings unavailable")
    monkeypatch.setattr(exports, "save_status", unavailable)
    with pytest.raises(core.ToolError, match="saved"):
        exports.export_backup(run, target)
    assert target.is_file()
    assert Path(str(target) + ".sha256").is_file()
    restored = tmp_path / "restored"
    with zipfile.ZipFile(target) as z:
        z.extractall(restored)
    assert core.verify_run(restored) == []


def test_broken_confirmation_receipt_does_not_suppress_backup_reminder(desktop):
    run = generate(desktop, "Alpha")
    receipt = {"state": "confirmed_by_user", "bundle_sha256": core.sha256_file(run / "evidence.json")}
    (run / "backup_status.json").write_text(json.dumps(receipt))
    assert exports.backup_status(run)["state"] == "backup_required"


def test_snapshot_and_restored_bytes_match_an_independent_file_inventory(tmp_path):
    root = tmp_path / "projects"
    plain = root / "Folder with spaces"
    expected = {"zero.txt": b"", "data.bin": bytes(range(256)), "unicode-\u03bb.txt": b"unicode name",
                "nested/deeper/file.txt": b"nested", ".gitignore": b"*.secret\n!keep.secret\n",
                "keep.secret": b"intentionally included"}
    write(plain, {**expected, "omit.secret": "ignored", "node_modules/dep.js": "dependency",
                  "venv/pyvenv.cfg": "home = ignored", "venv/lib.py": "dependency"})
    nested = plain / "nested-repo"
    write(nested, {"child.txt": "nested repository"})
    git(nested, "init", "-q")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in plain.rglob("*") if p.is_file()}
    result = core.generate(core.scan_roots([], [plain]), tmp_path / "runs", snapshot=True, log=lambda _: None)
    entry = result["results"][0]
    actual = core.read_manifest((result["out_dir"] / entry["manifest"]).read_bytes())
    assert actual == {name: hashlib.sha256(content).hexdigest() for name, content in expected.items()}
    assert entry["file_count"] == len(expected)
    assert entry["total_bytes"] == sum(map(len, expected.values()))
    with zipfile.ZipFile(result["out_dir"] / entry["snapshot"]) as z:
        assert {name: z.read(name) for name in z.namelist()} == expected
    assert all((p.read_bytes(), p.stat().st_mtime_ns) == state for p, state in before.items())


def test_confirming_new_run_does_not_hide_an_older_pending_backup(desktop, tmp_path, monkeypatch):
    alpha = generate(desktop, "Alpha")
    beta = generate(desktop, "Beta")
    exports.export_backup(beta, tmp_path / "beta.zip")
    exports.confirm_backup(beta, "cloud")
    desktop.refresh_backup()
    assert "other run(s)" in desktop.backup_var.get()
    asked, destroyed = [], []
    with monkeypatch.context() as patch:
        patch.setattr(gui.messagebox, "askyesno", lambda title, text: asked.append(text) or False)
        patch.setattr(desktop, "destroy", lambda: destroyed.append(True))
        desktop.close()
    assert asked and not destroyed
    button(desktop, "Previous runs").invoke()
    window = next(w for w in desktop.winfo_children() if isinstance(w, tk.Toplevel))
    rows = next(w for w in window.winfo_children()[0].winfo_children() if isinstance(w, tk.Listbox))
    rows.selection_clear(0, "end")
    rows.selection_set(1)
    button(desktop, "Open selected run").invoke()
    assert desktop.out_dir == alpha
    assert "1. Alpha" in desktop.table_text.get("1.0", "end")
    assert {str(alpha), str(beta)} == set(json.loads(gui.SETTINGS.read_text())["runs"])


def test_restart_with_missing_disclosure_clears_old_text_but_keeps_evidence(desktop):
    alpha = generate(desktop, "Alpha")
    generate(desktop, "Beta")
    (alpha / "disclosure.md").unlink()
    saved = json.loads(gui.SETTINGS.read_text())
    saved["last_run"] = str(alpha)
    gui.SETTINGS.write_text(json.dumps(saved))
    desktop.restore_last_run()
    assert desktop.out_dir == alpha
    assert "not included" in desktop.table_text.get("1.0", "end")
    assert "Beta" not in desktop.table_text.get("1.0", "end")
    assert "Alpha" in desktop.email_text.get("1.0", "end")


def test_write_failure_during_backup_removes_only_new_partial_files(desktop, tmp_path, monkeypatch):
    run = generate(desktop, "Alpha")
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"previous backup")
    target = tmp_path / "new.zip"
    original = zipfile.ZipFile.open
    writes = []
    def fail_second_write(self, name, mode="r", *args, **kwargs):
        if mode == "w":
            writes.append(name)
            if len(writes) == 2:
                raise OSError("disk full")
        return original(self, name, mode, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, "open", fail_second_write)
    with pytest.raises(OSError, match="disk full"):
        exports.export_backup(run, target)
    assert not target.exists()
    assert not Path(str(target) + ".sha256").exists()
    assert existing.read_bytes() == b"previous backup"
    assert exports.backup_status(run)["state"] == "backup_required"


def test_existing_checksum_is_not_overwritten(desktop, tmp_path):
    run = generate(desktop, "Alpha")
    target = tmp_path / "private.zip"
    sidecar = Path(str(target) + ".sha256")
    sidecar.write_text("keep this checksum")
    with pytest.raises(core.ToolError, match="already exists"):
        exports.export_backup(run, target)
    assert not target.exists()
    assert sidecar.read_text() == "keep this checksum"


def test_gui_error_callback_does_not_stop_future_background_results(desktop):
    def broken_display(result):
        raise ValueError("cannot display")
    desktop.work(lambda: 1, broken_display)
    settle(desktop)
    assert "cannot display" in desktop.dialogs
    results = []
    desktop.work(lambda: 2, results.append)
    settle(desktop)
    assert results == [2]


def test_generate_is_ignored_while_busy(desktop, monkeypatch):
    click_row(desktop, "Alpha")
    calls = []
    monkeypatch.setattr(desktop, "work", lambda *args: calls.append(args))
    desktop.busy = True
    desktop.generate()
    desktop.busy = False
    assert calls == []


def test_bad_saved_source_entry_does_not_drop_valid_folders(desktop):
    source = desktop.sources[0][1]
    gui.SETTINGS.write_text(json.dumps({"folders": [source, None, 3], "projects": "not a list"}))
    assert desktop.load_sources() == [("folders", source)]


def test_failed_settings_replacement_preserves_previous_settings(desktop, monkeypatch):
    original = gui.SETTINGS.read_bytes()
    def denied(*args):
        raise PermissionError("destination is unavailable")
    monkeypatch.setattr(gui.os, "replace", denied)
    desktop.save_sources()
    assert gui.SETTINGS.read_bytes() == original
    assert "Settings could not be saved" in desktop.status_var.get()
    assert not list(gui.SETTINGS.parent.glob(".priorfolio-settings-*.tmp"))


def test_larger_system_font_does_not_clip_action_labels(desktop):
    from tkinter import font
    desktop.geometry("820x840")
    font.nametofont("TkDefaultFont", root=desktop).configure(size=14)
    desktop.update()
    for label in ("Generate", "Verify a run", "Open output folder", "Export backup", "Confirm remote backup", "Previous runs"):
        widget = button(desktop, label)
        assert widget.winfo_width() >= widget.winfo_reqwidth(), label
        assert widget.winfo_rootx() + widget.winfo_width() <= desktop.winfo_rootx() + desktop.winfo_width(), label
        # Tk lists siblings from bottom to top in stacking order. A row frame
        # above a control obscures it and intercepts clicks, even at full width.
        container = widget.pack_info()["in"]
        siblings = widget.master.winfo_children()
        assert siblings.index(widget) > siblings.index(container), label
    for widget in desktop.winfo_children()[-1].winfo_children():
        assert widget.winfo_viewable()
        assert widget.winfo_rooty() + widget.winfo_height() <= desktop.winfo_rooty() + desktop.winfo_height()


def test_deleted_run_folder_does_not_block_closing_forever(desktop, tmp_path, monkeypatch):
    alpha = generate(desktop, "Alpha")
    beta = generate(desktop, "Beta")
    exports.export_backup(beta, tmp_path / "beta.zip")
    exports.confirm_backup(beta, "cloud")
    shutil.rmtree(alpha)  # the user removed a run they no longer want
    desktop.refresh_backup()
    assert desktop.pending_runs() == []
    assert "other run(s)" not in desktop.backup_var.get()
    asked, destroyed = [], []
    with monkeypatch.context() as patch:
        patch.setattr(gui.messagebox, "askyesno", lambda title, text: asked.append(text) or False)
        patch.setattr(desktop, "destroy", lambda: destroyed.append(True))
        desktop.close()
    assert not asked and destroyed
    button(desktop, "Previous runs").invoke()
    window = next(w for w in desktop.winfo_children() if isinstance(w, tk.Toplevel))
    rows = next(w for w in window.winfo_children()[0].winfo_children() if isinstance(w, tk.Listbox))
    assert "FOLDER NOT FOUND" in rows.get(1)
    window.destroy()


def test_timestamps_are_on_by_default_and_the_window_says_they_reveal_your_ip(desktop):
    assert desktop.timestamp_default is True
    assert "Timestamps reveal your IP" in str(desktop.notice.cget("text"))
    assert desktop.notice.winfo_viewable()
    assert "IP address" in " ".join(gui.HELP_TEXT.split())


def secret_run(desktop, tmp_path):
    write(tmp_path / "projects" / "Alpha", {".env": "TOKEN=private\n", "deploy.pem": "KEY\n"})
    button(desktop, "Rescan").invoke()
    settle(desktop)
    return generate(desktop, "Alpha")


def save_dialogs(monkeypatch, answers):
    """Answer each save dialog in turn; returns the dialogs' keyword arguments."""
    asked, answers = [], list(answers)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename",
                        lambda **kw: asked.append(kw) or (str(answers.pop(0)) if answers else ""))
    return asked


def test_secrets_lead_to_a_full_copy_and_a_cloud_copy(desktop, tmp_path, monkeypatch):
    run = secret_run(desktop, tmp_path)
    asked = save_dialogs(monkeypatch, [tmp_path / "full.zip", tmp_path / "cloud.zip"])
    button(desktop, "Export backup").invoke()
    window = desktop.copies_window
    rows = next(w for w in window.winfo_children()[0].winfo_children() if isinstance(w, tk.Listbox))
    assert rows.get(0, "end") == ("Alpha: .env", "Alpha: deploy.pem") and len(rows.curselection()) == 2
    assert not asked, "no file dialog before choosing copies"
    button(desktop, "Export full and cloud copies").invoke()
    settle(desktop)
    assert [kw["initialfile"] for kw in asked] == [f"PriorFolio-{run.name}-full.zip",
                                                    f"PriorFolio-{run.name}-cloud.zip"]
    assert "keep it offline" in asked[0]["title"] and "2 file(s) withheld" in asked[1]["title"]
    status = exports.backup_status(run)
    assert status["withheld"] == ["Alpha: .env", "Alpha: deploy.pem"]
    assert status["full_copy"]["archive"] == str(tmp_path / "full.zip")
    assert "cloud copy without 2 file(s); the full copy was exported" in desktop.backup_var.get()
    with zipfile.ZipFile(tmp_path / "cloud.zip") as z:
        assert "WITHHELD.txt" in z.namelist()


def test_unselected_files_stay_in_the_cloud_copy(desktop, tmp_path, monkeypatch):
    run = secret_run(desktop, tmp_path)
    save_dialogs(monkeypatch, [tmp_path / "full.zip", tmp_path / "cloud.zip"])
    button(desktop, "Export backup").invoke()
    rows = next(w for w in desktop.copies_window.winfo_children()[0].winfo_children()
                if isinstance(w, tk.Listbox))
    rows.selection_clear(1)
    button(desktop, "Export full and cloud copies").invoke()
    settle(desktop)
    assert exports.backup_status(run)["withheld"] == ["Alpha: .env"]


@pytest.mark.parametrize("choice, dialogs, exported", [
    ("Full copy only", 1, ["full.zip"]),
    ("Cancel", 0, []),
])
def test_full_copy_only_or_cancel(desktop, tmp_path, monkeypatch, choice, dialogs, exported):
    run = secret_run(desktop, tmp_path)
    asked = save_dialogs(monkeypatch, [tmp_path / "full.zip", tmp_path / "cloud.zip"])
    button(desktop, "Export backup").invoke()
    button(desktop, choice).invoke()
    settle(desktop)
    assert len(asked) == dialogs
    assert sorted(p.name for p in tmp_path.glob("*.zip")) == exported
    assert "withheld" not in exports.backup_status(run)


def test_cancelling_the_cloud_dialog_still_saves_the_full_copy(desktop, tmp_path, monkeypatch):
    run = secret_run(desktop, tmp_path)
    save_dialogs(monkeypatch, [tmp_path / "full.zip"])  # the second dialog is cancelled
    button(desktop, "Export backup").invoke()
    button(desktop, "Export full and cloud copies").invoke()
    settle(desktop)
    assert exports.backup_status(run)["archive"] == str(tmp_path / "full.zip")
    assert not (tmp_path / "cloud.zip").exists()


def test_without_secrets_export_goes_straight_to_one_private_copy(desktop, tmp_path, monkeypatch):
    run = generate(desktop, "Alpha")
    asked = save_dialogs(monkeypatch, [tmp_path / "private.zip"])
    button(desktop, "Export backup").invoke()
    settle(desktop)
    assert desktop.copies_window is None
    assert len(asked) == 1 and asked[0]["initialfile"] == f"PriorFolio-{run.name}-private.zip"
    assert "7-Zip" in desktop.status_var.get()


def test_row_help_explains_a_skipped_git_status():
    text = gui.explain({"path": "/p", "state": "repo config can run programs (filter.x.clean); git status skipped",
                        "git_error": None, "is_repo": True, "commit": "abc", "modified": 0, "untracked": 0,
                        "remote": None, "unpushed": None, "config_warning": "filter.x.clean"})
    assert "filter.x.clean" in text and "never starts them" in text


# ------------------------------------------------- command line and shared settings

def cli(*args, settings=None):
    """Run the CLI in a subprocess. Timestamps stay off; settings go to the test file."""
    import os
    import subprocess
    import sys
    env = dict(os.environ)
    if settings:
        env["PRIORFOLIO_SETTINGS"] = str(settings)
    args = [str(a) for a in args]
    if "--select" in args:
        args.append("--no-timestamp")  # on by default; tests stay offline
    return subprocess.run([sys.executable, str(Path(core.__file__)), *args], env=env,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    write(root / "Alpha", {"work.txt": "alpha\n"})
    write(root / "Beta", {"work.txt": "beta\n"})
    return root


def test_cli_details_are_saved_shown_and_reach_the_disclosure(projects, tmp_path, isolated_settings):
    edited = cli("--root", projects, "--details", "Alpha", "--title", "Widget", "--description",
                 "A widget  ", "--contribution", "All of it", "--period", "2025", "--references", "example.com")
    assert edited.returncode == 0, edited.stderr
    assert "Public title: Widget" in edited.stdout and "Description: A widget\n" in edited.stdout
    alpha = str((projects / "Alpha").resolve())
    assert exports.saved_details()[alpha]["description"] == "A widget"

    # Changing one field keeps the others; an empty string clears a field.
    cli("--root", projects, "--details", "1", "--period", "")
    assert exports.saved_details()[alpha] == {"title": "Widget", "description": "A widget",
                                              "contribution": "All of it", "period": "", "references": "example.com"}
    shown = cli("--root", projects, "--details", "all")
    assert "Work dates or period: [not set]" in shown.stdout and "[not set; uses Beta]" in shown.stdout

    made = cli("--root", projects, "--select", "all", "--no-snapshot", "--out", tmp_path / "runs")
    assert made.returncode == 0, made.stderr
    assert "no description or contribution in their disclosure: Beta." in made.stderr
    (run,) = (tmp_path / "runs").iterdir()
    disclosure = (run / "disclosure.md").read_text(encoding="utf-8")
    assert "## 1. Widget" in disclosure and "**My contribution:** All of it" in disclosure
    assert "## 2. Beta" in disclosure


def test_cli_details_rejects_bad_use(projects):
    many = cli("--root", projects, "--details", "all", "--title", "Same")
    assert many.returncode != 0 and "choose one to change" in many.stderr
    assert exports.saved_details() == {}
    alone = cli("--root", projects, "--title", "No project")
    assert alone.returncode == 2 and "require --details" in alone.stderr
    unknown = cli("--root", projects, "--details", "Gamma")
    assert unknown.returncode != 0 and "no such project: Gamma" in unknown.stderr


def test_cli_details_export_edit_import_round_trip(projects, tmp_path):
    cli("--root", projects, "--details", "Beta", "--title", "Kept title")
    target = tmp_path / "details.json"
    exported = cli("--root", projects, "--export-details", target)
    assert exported.returncode == 0, exported.stderr
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [p["name"] for p in data["projects"]] == ["Alpha", "Beta"]
    assert data["projects"][1]["title"] == "Kept title"
    assert cli("--root", projects, "--export-details", target).returncode != 0  # never overwrites

    data["projects"][0]["description"] = "Imported description"
    del data["projects"][1]["title"]  # left out: the saved title stays
    data["projects"][1]["contribution"] = "Imported contribution"
    target.write_text(json.dumps(data), encoding="utf-8")
    imported = cli("--import-details", target)
    assert imported.returncode == 0 and "2 project(s)" in imported.stdout, imported.stderr
    saved = exports.saved_details()
    assert saved[str((projects / "Alpha").resolve())]["description"] == "Imported description"
    beta = saved[str((projects / "Beta").resolve())]
    assert beta["title"] == "Kept title" and beta["contribution"] == "Imported contribution"


@pytest.mark.parametrize("content, message", [
    ("not json", "not valid JSON"),
    ('{"format": "other", "projects": []}', "not a PriorFolio details file"),
    ('{"format": "priorfolio-details-v1", "projects": [{"name": "x"}]}', 'needs a "path"'),
    ('{"format": "priorfolio-details-v1", "projects": [{"path": "PROJECT", "contributions": "x"}]}',
     "unknown field(s) contributions"),
    ('{"format": "priorfolio-details-v1", "projects": [{"path": "PROJECT", "title": 1}]}', "title must be text"),
    ('{"format": "priorfolio-details-v1", "projects": [{"path": "MISSING"}]}', "not a folder"),
])
def test_cli_import_details_rejects_bad_files_without_saving(projects, tmp_path, content, message):
    alpha = str((projects / "Alpha").resolve())
    content = content.replace("PROJECT", json.dumps(alpha)[1:-1]).replace(
        "MISSING", json.dumps(str(tmp_path / "gone"))[1:-1])
    target = tmp_path / "details.json"
    target.write_text(content, encoding="utf-8")
    result = cli("--import-details", target)
    assert result.returncode != 0 and message in result.stderr and "Traceback" not in result.stderr
    assert exports.saved_details() == {}


def test_cli_runs_lists_remembered_runs_and_backup_state(projects, tmp_path):
    assert "No remembered runs." in cli("--runs").stdout
    for name in ("Alpha", "Beta"):
        assert cli("--root", projects, "--select", name, "--out", tmp_path / "runs").returncode == 0
    first, second = sorted((tmp_path / "runs").iterdir())
    target = tmp_path / "backup.zip"
    assert cli("--backup", first, "--destination", target).returncode == 0
    assert cli("--confirm-backup", first, "--backup-method", "cloud").returncode == 0
    shutil.rmtree(second)
    listed = cli("--runs").stdout.splitlines()
    assert listed[0].startswith("FOLDER NOT FOUND") and listed[0].endswith(str(second.resolve()))
    assert listed[1].startswith("confirmed by you") and "need a remote backup" not in "\n".join(listed)


def test_desktop_sees_details_and_runs_saved_by_the_cli(desktop, tmp_path):
    root = tmp_path / "projects"
    settings = gui.SETTINGS
    assert cli("--root", root, "--details", "Alpha", "--title", "From the CLI",
               settings=settings).returncode == 0
    assert cli("--root", root, "--select", "Beta", "--no-snapshot", "--out", tmp_path / "cli-runs",
               settings=settings).returncode == 0
    (cli_run,) = (tmp_path / "cli-runs").iterdir()
    assert cli_run.resolve() in desktop.pending_runs()

    button(desktop, "Rescan").invoke()
    settle(desktop)
    alpha = next(p for p in desktop.projects if p["name"] == "Alpha")
    assert alpha["disclosure"]["title"] == "From the CLI"

    # Saving from the window keeps what the CLI wrote.
    generate(desktop, "Alpha")
    assert "From the CLI" in desktop.table_text.get("1.0", "end")
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert str(cli_run.resolve()) in saved["runs"]
    assert saved["details"][alpha["path"]]["title"] == "From the CLI"
    assert saved["folders"] == [str(root)]


def test_cli_verify_remembers_runs_whose_evidence_verifies(projects, tmp_path):
    made = cli("--root", projects, "--select", "Alpha", "--out", tmp_path / "runs",
               settings=tmp_path / "maker.json")  # a run made elsewhere, e.g. a restored copy
    assert made.returncode == 0, made.stderr
    (run,) = (tmp_path / "runs").iterdir()

    wrong = cli("--verify", run, "--expect", "0" * 64)
    assert wrong.returncode == 1 and "remembered" not in wrong.stdout
    assert exports.saved_runs() == []

    # Work continued on the project: the run itself is intact, so it is remembered.
    (projects / "Alpha" / "work.txt").write_text("changed\n")
    changed = cli("--verify", run, "--against-disk")
    assert changed.returncode == 1 and "Alpha: on disk changed: work.txt" in changed.stdout
    assert "This run is remembered" in changed.stdout
    assert exports.saved_runs() == [run.resolve()]
    assert cli("--runs").stdout.startswith("BACKUP REQUIRED")


def test_cli_verify_does_not_remember_a_damaged_run(projects, tmp_path):
    assert cli("--root", projects, "--select", "Alpha", "--out", tmp_path / "runs").returncode == 0
    (run,) = (tmp_path / "runs").iterdir()
    settings = tmp_path / "fresh.json"
    (snapshot,) = (run / "snapshots").glob("*.zip")
    snapshot.write_bytes(snapshot.read_bytes() + b"x")
    damaged = cli("--verify", run, "--against-disk", settings=settings)
    assert damaged.returncode == 1 and "snapshot zip does not match" in damaged.stdout
    assert "remembered" not in damaged.stdout and not settings.exists()

    unreadable = cli("--verify", tmp_path, settings=settings)
    assert unreadable.returncode == 1 and not settings.exists()
