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
