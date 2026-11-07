# SPDX-License-Identifier: GPL-3.0-only
"""Windowed desktop launcher, with an isolated startup check for release builds."""
import json
from pathlib import Path
import sys
import tempfile
import time

import priorfolio_gui as gui
from runtime import prepare


def smoke_test(report):
    errors = []
    original_settings = gui.SETTINGS
    original_error = gui.messagebox.showerror
    app = None
    try:
        with tempfile.TemporaryDirectory(prefix="priorfolio-desktop-check-") as temporary:
            folder = Path(temporary)
            project = folder / "example"
            project.mkdir()
            (project / "design.txt").write_text("Synthetic desktop build check.\n")
            gui.SETTINGS = folder / "settings.json"
            gui.SETTINGS.write_text(json.dumps({"projects": [str(project)]}))
            gui.messagebox.showerror = lambda title, message, **kw: errors.append(message)
            app = gui.App()
            app.withdraw()
            app.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
            deadline = time.monotonic() + 30
            while app.busy and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            app.update()
            if app.busy or errors or len(app.projects) != 1:
                raise RuntimeError(f"Desktop startup check failed: {errors}")
            report.write_text(json.dumps({"started": True, "projects": len(app.projects),
                                          "tk": app.tk.call("info", "patchlevel")}), encoding="utf-8")
    except Exception as error:
        report.write_text(json.dumps({"error": str(error)}), encoding="utf-8")
        raise SystemExit(1)
    finally:
        if app is not None:
            app.destroy()
        gui.SETTINGS = original_settings
        gui.messagebox.showerror = original_error


if __name__ == "__main__":
    prepare()
    if len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        smoke_test(Path(sys.argv[2]))
    else:
        gui.main()
