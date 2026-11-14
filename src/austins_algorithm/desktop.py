"""Launch the Windows Workbench or run its local acceptance smoke check."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import traceback

from .workbench_release import DESKTOP_VERSION


def _record_startup_error(exc: Exception) -> Path | None:
    directory = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ChronoRealm" / "AustinWorkbench" / "diagnostics"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"startup-{stamp}.log"
        path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
        return path
    except OSError:
        return None


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--future-reader-worker":
        if len(argv) != 5 or argv[1] != "_worker":
            raise SystemExit(2)
        from future_reader.live_failover import main as worker_main

        raise SystemExit(worker_main(argv[1:]))
    parser = argparse.ArgumentParser(prog="austins-algorithm-workbench", description=__doc__)
    parser.add_argument("--version", action="version", version=DESKTOP_VERSION)
    parser.add_argument("--smoke", type=Path, metavar="DIRECTORY", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    import tkinter as tk
    from tkinter import messagebox

    root = None
    try:
        from .workbench import Workbench

        if sys.platform == "win32":
            from ctypes import windll
            try:
                windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        root = tk.Tk()
        def callback_error(error_type, error, tb):
            path = _record_startup_error(error)
            detail = f"\n\nDetails were saved to {path}" if path is not None else ""
            messagebox.showerror("Austin's Algorithm Workbench", f"{error}{detail}", parent=root)

        if args.smoke is None:
            root.report_callback_exception = callback_error
        icon = Path(__file__).parent / "workbench_assets" / "app.ico"
        if sys.platform == "win32" and icon.is_file():
            root.iconbitmap(default=str(icon))
        app = Workbench(root)
        if args.smoke is not None:
            args.smoke.mkdir(parents=True, exist_ok=True)
            status = app.smoke(args.smoke)
            with (args.smoke / "desktop-smoke.json").open("x", encoding="utf-8") as output:
                json.dump({"desktop_version": DESKTOP_VERSION, "frozen": bool(getattr(sys, "frozen", False)), **status},
                          output, indent=2)
                output.write("\n")
            root.destroy()
            return
        root.mainloop()
    except Exception as exc:
        path = _record_startup_error(exc)
        if args.smoke is not None:
            args.smoke.mkdir(parents=True, exist_ok=True)
            (args.smoke / "desktop-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if root is not None:
                root.destroy()
            raise
        detail = f"\n\nDetails were saved to {path}" if path is not None else ""
        messagebox.showerror("Austin's Algorithm Workbench", f"The workbench could not start.\n\n{exc}{detail}", parent=root)
        if root is not None:
            root.destroy()
        raise SystemExit(1) from exc
