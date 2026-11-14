import gc
import json
import time
import tkinter as tk
import tkinter.font

import pytest

import priorfolio as sa

gui = pytest.importorskip("priorfolio_gui")


@pytest.fixture
def app(workspace, tmp_path, monkeypatch):
    """The real window, pointed at the test workspace, with dialogs captured."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"folders": [str(workspace)]}))
    monkeypatch.setattr(gui, "SETTINGS", settings)
    dialogs = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda title, message: dialogs.append(message))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda title, message: dialogs.append(message))
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, message: dialogs.append(message))
    # Tk objects must be freed on the thread that made them. Clear out the
    # previous test's window here, then keep the collector from running on the
    # scan/generate worker thread while this one is alive.
    gc.collect()
    gc.disable()
    try:
        window = gui.App()
    except tk.TclError as exc:
        gc.enable()
        if "no display" in str(exc).lower() or "couldn't connect to display" in str(exc).lower():
            pytest.skip("no display available")
        raise
    window.out_base = tmp_path / "gui-out"
    window.snapshot_var.set(False)
    window.timestamp_var.set(False)
    window.dialogs = dialogs
    settle(window)
    try:
        yield window
    finally:
        window.destroy()
        gc.enable()


def settle(window, limit=60):
    """Run the event loop until background work has finished."""
    deadline = time.time() + limit
    window.update()
    while time.time() < deadline:
        window.update()
        if not window.busy and window.events.empty():
            break
        time.sleep(0.01)
    window.update()
    assert not window.busy, "background work did not finish"


def button(window, text):
    def search(widget):
        for child in widget.winfo_children():
            if child.winfo_class() == "TButton" and child.cget("text") == text:
                return child
            found = search(child)
            if found:
                return found
    found = search(window)
    assert found, f"no button labelled {text!r}"
    return found


def click_row(window, name):
    index = next(i for i, p in enumerate(window.projects) if p["name"] == name)
    deadline = time.monotonic() + 2
    while True:
        # Aqua applies native geometry changes asynchronously. Let pending
        # status/layout changes settle before scrolling the next row into view.
        window.update()
        window.tree.see(str(index))
        window.update()
        bounds = window.tree.bbox(str(index), "name")
        if bounds:
            break
        assert time.monotonic() < deadline, (
            f"Row {name!r} is not visible: tree={window.tree.winfo_geometry()}, "
            f"window={window.winfo_geometry()}, scroll={window.tree.yview()}")
        time.sleep(0.01)
    x, y, width, height = bounds
    window.tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + height // 2)
    window.tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + height // 2)
    window.update()
    return index


def test_startup_lists_every_project_with_its_state(app, workspace):
    assert [p["name"] for p in app.projects] == [p["name"] for p in sa.scan_roots([workspace])]
    rows = {app.tree.set(i, "name"): app.tree.item(i) for i in app.tree.get_children()}
    assert rows["plain"]["tags"] == ("nohash",) or "nohash" in rows["plain"]["tags"]
    assert "dirty" in rows["repo"]["tags"]
    assert rows["plain"]["values"][4] == "NOT A GIT REPO"


def test_window_fits_on_the_screen(app):
    assert app.winfo_width() <= app.winfo_screenwidth()
    assert app.winfo_height() <= app.winfo_screenheight()
    for label in ("Add folder", "Add single project", "Remove", "Rescan", "Generate",
                  "Verify a run", "Open output folder"):
        widget = button(app, label)
        right_edge = widget.winfo_rootx() + widget.winfo_width()
        assert right_edge <= app.winfo_rootx() + app.winfo_width(), label


@pytest.mark.parametrize("size", [None, 24])
def test_rows_and_columns_fit_their_text(size, monkeypatch, request):
    """Large or high-DPI fonts must not be clipped by fixed row heights or column widths."""
    if size:
        create = tk.Tk.__init__

        def large_fonts(self, *args, **kwargs):
            create(self, *args, **kwargs)
            for name in ("TkDefaultFont", "TkHeadingFont"):
                tk.font.nametofont(name, root=self).configure(size=size)

        monkeypatch.setattr(tk.Tk, "__init__", large_fonts)
    app = request.getfixturevalue("app")
    body = tk.font.nametofont("TkDefaultFont")
    heading = tk.font.nametofont("TkHeadingFont")
    app.update_idletasks()
    row = app.tree.get_children()[0]
    app.tree.see(row)
    app.update()
    assert app.tree.bbox(row)[3] >= body.metrics("linespace")
    for col in ("commit", "date"):
        value = app.tree.set(row, col)
        needed = max(body.measure(value), heading.measure(app.tree.heading(col, "text")))
        assert app.tree.column(col, "width") >= needed, col


def test_window_too_short_for_both_panes_still_settles(app, monkeypatch):
    """When the panes' minimums cannot both fit, the divider must settle (#2)."""
    calls = []
    fit = gui.App.fit_panes

    def counted(self):
        calls.append(self.panes.sashpos(0))
        if len(calls) <= 200:  # past this, stop the loop so the test fails, not hangs
            fit(self)

    monkeypatch.setattr(gui.App, "fit_panes", counted)
    app.minsize(1, 1)
    app.geometry(f"{app.winfo_width()}x300")
    app.update()
    assert len(calls) <= 200, f"fit_panes looped; divider positions: {calls[-5:]}"
    calls.clear()
    app.update()
    assert len(calls) <= 1


def test_clicking_rows_ticks_and_unticks_them(app):
    index = click_row(app, "plain")
    assert app.checked == {index} and app.tree.set(str(index), "sel") == gui.CHECKED
    click_row(app, "plain")
    assert app.checked == set() and app.tree.set(str(index), "sel") == gui.UNCHECKED


@pytest.mark.parametrize("screen", [(1024, 768), (880, 670)])
def test_small_screen_keeps_project_rows_and_scrollbar_usable(screen, monkeypatch, request):
    monkeypatch.setattr(tk.Tk, "winfo_screenwidth", lambda self: screen[0])
    monkeypatch.setattr(tk.Tk, "winfo_screenheight", lambda self: screen[1])
    app = request.getfixturevalue("app")
    first = click_row(app, "plain")
    second = click_row(app, "repo")
    assert app.checked == {first, second}
    _, y, _, height = app.tree.bbox(str(second))
    assert app.tree.winfo_height() >= 3 * height
    assert y + height <= app.tree.winfo_height()
    scrollbar = next(child for child in app.tree.master.winfo_children()
                     if child.winfo_class() == "TScrollbar")
    assert scrollbar.winfo_viewable()
    assert scrollbar.winfo_width() >= scrollbar.winfo_reqwidth()
    for label in ("Generate", "Verify a run", "Export backup", "Previous runs"):
        widget = button(app, label)
        assert widget.winfo_viewable(), label
        assert widget.winfo_rooty() + widget.winfo_height() <= app.winfo_rooty() + app.winfo_height(), label


def test_row_selection_does_not_depend_on_native_theme_region_labels(app, monkeypatch):
    monkeypatch.setattr(app.tree, "identify_region", lambda *args: "tree")
    index = click_row(app, "plain")
    assert app.checked == {index}


def test_all_and_none_buttons(app):
    button(app, "All").invoke()
    assert len(app.checked) == len(app.projects) == 7
    button(app, "None").invoke()
    assert app.checked == set()


def test_generate_with_nothing_ticked_asks_for_a_selection(app):
    button(app, "Generate").invoke()
    assert app.dialogs == ["Tick at least one project first."]
    assert not app.out_base.exists()


def test_generate_fills_both_text_boxes_and_the_run_verifies(app):
    click_row(app, "plain")
    click_row(app, "repo")
    button(app, "Generate").invoke()
    settle(app)

    assert app.dialogs == []
    email = app.email_text.get("1.0", "end-1c")
    schedule = app.table_text.get("1.0", "end-1c")
    assert email == (app.out_dir / "email.txt").read_text(encoding="utf-8")
    assert schedule == (app.out_dir / "disclosure.md").read_text(encoding="utf-8")
    assert "1. plain" in email and "2. repo" in email
    assert str(button(app, "Open output folder").cget("state")) == "normal"
    assert sa.verify_run(app.out_dir, against_disk=True) == []


def test_copy_button_puts_the_exact_email_on_the_clipboard(app):
    click_row(app, "clean")
    button(app, "Generate").invoke()
    settle(app)
    app.copy(app.email_text)
    assert app.clipboard_get() == (app.out_dir / "email.txt").read_text(encoding="utf-8")


def test_add_single_project_adds_one_row_and_is_remembered(app, workspace, monkeypatch):
    target = workspace / "plain" / "src"
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(target))
    button(app, "Add single project").invoke()
    settle(app)

    assert ("projects", str(target)) in app.sources
    assert [p["name"] for p in app.projects].count("src") == 1
    assert len(app.projects) == 8
    assert app.folders.get("end") == f"Single project:  {target}"
    assert json.loads(gui.SETTINGS.read_text())["projects"] == [str(target)]

    button(app, "Add single project").invoke()  # same folder again: no duplicate
    settle(app)
    assert len(app.sources) == 2 and len(app.projects) == 8


def test_add_folder_then_remove_it(app, tmp_path, monkeypatch):
    other = tmp_path / "second"
    (other / "tool").mkdir(parents=True)
    (other / "tool" / "t.py").write_text("pass\n")
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(other))
    button(app, "Add folder").invoke()
    settle(app)
    assert "tool" in [p["name"] for p in app.projects]

    app.folders.selection_set(1)
    button(app, "Remove").invoke()
    settle(app)
    assert "tool" not in [p["name"] for p in app.projects]
    assert json.loads(gui.SETTINGS.read_text())["folders"] == [app.sources[0][1]]


def test_cancelling_the_folder_dialog_changes_nothing(app, monkeypatch):
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: "")
    button(app, "Add single project").invoke()
    settle(app)
    assert len(app.sources) == 1 and len(app.projects) == 7


def test_every_control_has_hover_help(app):
    def controls(widget):
        for child in widget.winfo_children():
            if child.winfo_class() in ("TButton", "TCheckbutton", "Listbox"):
                yield child
            yield from controls(child)

    missing = [w.cget("text") if w.winfo_class() != "Listbox" else "Sources list"
               for w in controls(app) if not w.bind("<Enter>")]
    assert missing == []
    assert set(gui.COLUMN_HELP) == set(app.tree["columns"])


def test_row_hover_text_explains_each_state(app):
    found = {p["name"]: gui.explain(p) for p in app.projects}
    assert "Not a git repository" in found["plain"]
    assert "nothing committed yet" in found["fresh"]
    assert "1 tracked file(s) changed, 1 new file(s)" in found["repo"]
    assert "No remote is configured" in found["clean"]
    assert all(p["path"] in found[p["name"]] for p in app.projects)


def test_help_window_opens_once_and_covers_the_controls(app):
    button(app, "ⓘ How it works").invoke()
    first = app.help_window
    button(app, "ⓘ How it works").invoke()
    assert app.help_window is first and first.winfo_exists()
    for term in ("Add single project", "Content hash", "Bundle hash", "Zip snapshots",
                 "Request timestamps", "evidence_bundle.zip", "NOT A GIT REPO"):
        assert term in gui.HELP_TEXT, term


# ------------------------------------------------------ warnings and verify

def test_unreadable_repo_shows_as_an_error_row_and_generate_warns(app, workspace):
    (workspace / "plain" / ".git").mkdir()  # an unusable repository
    button(app, "Rescan").invoke()
    settle(app)
    row = next(p for p in app.projects if p["name"] == "plain")
    assert row["state"].startswith("GIT ERROR")
    assert "Git could not read this repository" in gui.explain(row)
    index = app.projects.index(row)
    assert "nohash" in app.tree.item(str(index))["tags"]

    click_row(app, "plain")
    button(app, "Generate").invoke()
    settle(app)
    assert len(app.dialogs) == 1 and "Some content may be missing" in app.dialogs[0]
    assert "WARNING(S)" in app.status_var.get()
    assert "WARNING" in app.table_text.get("1.0", "end")
    assert sa.verify_run(app.out_dir) == []


def test_generate_after_a_project_folder_was_deleted(app, workspace):
    import shutil
    click_row(app, "plain")
    click_row(app, "clean")
    shutil.rmtree(workspace / "plain")
    button(app, "Generate").invoke()
    settle(app)
    assert any("folder no longer exists" in d for d in app.dialogs)
    assert "2. plain" in app.email_text.get("1.0", "end") or "1. plain" in app.email_text.get("1.0", "end")
    assert sa.verify_run(app.out_dir) == []


def test_verify_button_reports_intact_evidence_and_later_changes(app, workspace, monkeypatch):
    app.compare_var.set(True)
    click_row(app, "plain")
    button(app, "Generate").invoke()
    settle(app)
    run_dir = app.out_dir
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(run_dir))

    button(app, "Verify a run").invoke()
    settle(app)
    assert "EVIDENCE: OK" in app.verify_report
    assert "every hashed file is still identical" in app.verify_report
    assert (run_dir / "BUNDLE_SHA256.txt").read_text().split()[0] in app.verify_report

    (workspace / "plain" / "main.py").write_text("edited later\n")
    button(app, "Verify a run").invoke()
    settle(app)
    assert "EVIDENCE: OK" in app.verify_report
    assert "1 difference(s) since this run" in app.verify_report
    assert "plain: on disk changed: main.py" in app.verify_report

    manifest = run_dir / "manifests" / "plain.sha256"
    manifest.write_bytes(manifest.read_bytes() + b"tampered\n")
    button(app, "Verify a run").invoke()
    settle(app)
    assert "EVIDENCE: 1 PROBLEM(S)" in app.verify_report
    assert "problem(s) found" in app.status_var.get()


def test_verify_button_on_a_folder_that_is_not_a_run(app, tmp_path, monkeypatch):
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(tmp_path))
    button(app, "Verify a run").invoke()
    settle(app)
    assert "evidence.json is unreadable" in app.verify_report
    assert app.dialogs == []


def test_cancelling_verify_does_nothing(app, monkeypatch):
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: "")
    button(app, "Verify a run").invoke()
    settle(app)
    assert not hasattr(app, "verify_report")


def test_scan_error_is_shown_not_raised(app, monkeypatch):
    monkeypatch.setattr(sa.shutil, "which", lambda name: None)
    button(app, "Rescan").invoke()
    settle(app)
    assert app.dialogs and "git was not found" in app.dialogs[0]
    assert str(button(app, "Generate").cget("state")) == "normal"


def test_saved_sources_that_no_longer_exist_are_dropped_at_startup(workspace, tmp_path, monkeypatch):
    import gc
    settings = tmp_path / "settings2.json"
    settings.write_text(json.dumps({"folders": [str(workspace), str(tmp_path / "gone")],
                                    "projects": [str(tmp_path / "also-gone")]}))
    monkeypatch.setattr(gui, "SETTINGS", settings)
    gc.collect()
    gc.disable()
    try:
        window = gui.App()
        try:
            settle(window)
            assert window.sources == [("folders", str(workspace))]
            assert len(window.projects) == 7
        finally:
            window.destroy()
    finally:
        gc.enable()


@pytest.mark.parametrize("content", ["", "not json", "[]", "null", '{"folders": 5}', '{"folders": [3, null]}'])
def test_corrupt_settings_file_falls_back_to_the_default(content, tmp_path, monkeypatch):
    settings = tmp_path / "settings3.json"
    settings.write_text(content)
    monkeypatch.setattr(gui, "SETTINGS", settings)
    loaded = gui.App.load_sources(None)
    assert loaded == []


def test_space_toggles_the_focused_project(app):
    row = click_row(app, "plain")
    app.tree.focus(str(row))
    app.tree.focus_force()
    deadline = time.monotonic() + 2
    while app.focus_get() != app.tree and time.monotonic() < deadline:
        app.update()
        time.sleep(0.01)
    assert app.focus_get() == app.tree
    for expected in (False, True):
        app.tree.event_generate("<KeyPress>", keysym="space", when="tail")
        app.tree.event_generate("<KeyRelease>", keysym="space", when="tail")
        app.update()
        assert (row in app.checked) == expected


def test_project_details_are_saved_and_reach_the_disclosure(app):
    click_row(app, "plain")
    button(app, "Project details").invoke()
    win = next(w for w in app.winfo_children() if isinstance(w, tk.Toplevel))
    frame = win.winfo_children()[0]
    fields = [w for w in frame.winfo_children() if isinstance(w, tk.Text)]
    for widget, value in zip(fields, ["Example invention", "Example description", "My contribution", "2025", "example.com"]):
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
    button(app, "Save details").invoke()
    button(app, "Generate").invoke()
    settle(app)
    assert "Example invention" in app.table_text.get("1.0", "end")
    assert "Example description" in app.table_text.get("1.0", "end")
    saved = json.loads(gui.SETTINGS.read_text())
    assert any(d.get("title") == "Example invention" for d in saved["details"].values())


def test_export_then_explicit_remote_confirmation(app, tmp_path, monkeypatch):
    app.snapshot_var.set(True)
    click_row(app, "clean")
    button(app, "Generate").invoke()
    settle(app)
    assert "BACKUP REQUIRED" in app.backup_var.get()
    destination = tmp_path / "cloud-backup.zip"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kw: str(destination))
    button(app, "Export backup").invoke()
    settle(app)
    assert destination.is_file()
    assert "exported" in app.backup_var.get()
    assert gui.exports.backup_status(app.out_dir)["state"] == "exported"
    button(app, "Confirm remote backup").invoke()
    win = next(w for w in app.winfo_children() if isinstance(w, tk.Toplevel))
    confirm = button(app, "Record confirmation")
    assert str(confirm.cget("state")) == "disabled"
    frame = win.winfo_children()[0]
    acknowledgement = next(w for w in frame.winfo_children() if w.winfo_class() == "TCheckbutton")
    acknowledgement.invoke()
    confirm.invoke()
    assert gui.exports.backup_status(app.out_dir)["state"] == "confirmed_by_user"
    assert "confirmed by you" in app.backup_var.get()


def test_metadata_verification_labels_its_limited_scope(app, tmp_path, monkeypatch):
    import zipfile
    app.snapshot_var.set(True)
    click_row(app, "clean")
    button(app, "Generate").invoke()
    settle(app)
    received = tmp_path / "received"
    with zipfile.ZipFile(app.out_dir / "evidence_bundle.zip") as z:
        z.extractall(received)
    app.metadata_var.set(True)
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **kw: str(received))
    button(app, "Verify a run").invoke()
    settle(app)
    assert "METADATA: OK" in app.verify_report
    assert "snapshots were NOT checked" in app.verify_report
    assert "Local project folders were not read" in app.verify_report
    assert "source snapshots not checked" in app.status_var.get()


def test_close_during_work_keeps_the_window_alive(app):
    app.busy = True
    app.close()
    assert app.winfo_exists()
    assert "Wait for the current operation" in app.dialogs[-1]
    app.busy = False


def test_pending_backup_and_disclosure_are_restored_from_settings(app):
    click_row(app, "plain")
    button(app, "Generate").invoke()
    settle(app)
    run_dir = app.out_dir
    assert json.loads(gui.SETTINGS.read_text())["last_run"] == str(run_dir)
    app.out_dir = None
    app.set_text(app.table_text, "")
    app.restore_last_run()
    assert app.out_dir == run_dir
    assert "BACKUP REQUIRED" in app.backup_var.get()
    assert "plain" in app.table_text.get("1.0", "end")


def test_close_can_be_cancelled_while_remote_backup_is_pending(app, monkeypatch):
    click_row(app, "plain")
    button(app, "Generate").invoke()
    settle(app)
    asked = []
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda title, text: asked.append(text) or False)
    app.close()
    assert app.winfo_exists()
    assert asked and "no confirmed remote backup" in asked[0]
