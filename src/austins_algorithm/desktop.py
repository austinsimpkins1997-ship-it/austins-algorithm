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


def _focus_smoke(app, directory):
    """Exercise hidden focus controls and the existing portable project path."""
    from .workbench_project import verify_export

    app.show_page("Overview")
    app.open_focus()
    panel = app.focus_panel
    if panel.window.state() != "withdrawn":
        raise RuntimeError("The automated check displayed the Focus window.")
    panel.goal.set("Synthetic work checkpoint")
    panel.next_action.set("Inspect the next input")
    if not panel.act("START"):
        raise RuntimeError("Focus did not start through its public control handler.")
    panel.note.set("Synthetic priority interruption")
    if not panel.act("INTERRUPT") or panel.project["focus"]["status"] != "PAUSED":
        raise RuntimeError("Focus did not retain the interruption checkpoint.")
    saved = directory / "focus-smoke.json"
    archive = directory / "focus-smoke.zip"
    if not app.save(saved) or not app.open(saved) or not app.export(archive):
        raise RuntimeError("Focus did not survive the project save, open and export path.")
    app.open_focus()
    resumed = app.focus_panel
    if not resumed.act("RESUME") or resumed.project["focus"]["status"] != "ACTIVE":
        raise RuntimeError("Focus did not resume from the reopened checkpoint.")
    resumed.window.destroy()
    return {"status": "PASS", "evidence_kind": "SYNTHETIC_UI_CHECK", "window_hidden": True,
            "verified_export": verify_export(archive)}


def _decision_smoke(app, directory):
    """Exercise Decision Review through its normal Tk handlers."""
    from .decision_review_project import verify_review_export

    weighted_before = app.session.report()
    app.show_page("Decision Review")
    panel = app.decision_review
    if not panel.optimize() or panel.report["result"]["cost"] != 9:
        raise RuntimeError("Decision Review did not find the shared preset's exact minimum.")
    panel.step_trace(3)
    if "RUN" not in panel.stasis_text.get("1.0", "end") or "STOP" not in panel.stasis_text.get("1.0", "end"):
        raise RuntimeError("Decision Review did not display the final Stasis comparison.")
    panel.prediction.set("7")
    panel.predict_button.invoke()
    panel.practice_options["q0"].set(True)
    panel.practice_options["q2"].set(True)
    panel.submit_button.invoke()
    if panel.current_practice["evidence"]["baseline"] != "passed":
        raise RuntimeError("Decision Review did not capture the practice baseline.")
    path = directory / "decision-smoke.decision.json"
    archive = directory / "decision-smoke.zip"
    if not app.save(path) or not app.open(path) or not panel.optimize():
        raise RuntimeError("Decision Review did not save and reopen through the workbench.")
    receipt = app.export(archive)
    if not receipt:
        raise RuntimeError("Decision Review did not export through the workbench.")
    verified = verify_review_export(archive, manifest_sha256=receipt["manifest_sha256"])
    if app.session.report() != weighted_before:
        raise RuntimeError("Decision Review changed the weighted simulation session.")
    return {"status": verified["status"], "cost": panel.report["result"]["cost"], "verified_export": verified,
            "baseline": panel.current_practice["evidence"]["baseline"], "weighted_session_preserved": True}


def _planning_smoke(app,directory):
    import time
    from .planning_jobs import JobStore,verify_export
    app.show_page('Planning')
    panel=app.planning
    panel.store.close();panel.store=JobStore(directory/'planning-jobs')
    try:
        panel.start();deadline=time.monotonic()+25
        while panel.active() and time.monotonic()<deadline:
            app.root.update();time.sleep(.05)
        view=panel.poll()
        if view['status']!='OPTIMAL':raise RuntimeError('Native Planning worker did not establish the bounded optimum')
        project=directory/'planning-smoke.planning.json';archive=directory/'planning-smoke.zip'
        panel.save(project);panel.open(project)
        # Reopen the persisted result explicitly; opening a project never runs it.
        panel.selected_job.set(panel.jobs['values'][-1]);panel.open_job();panel.export(archive)
        return {'status':'PASS','overview':view,'export':verify_export(archive),'window_hidden':app.root.state()=='withdrawn'}
    finally:panel.close()


def _receipt_smoke(panel, files):
    rehearsal, rehearsal_pin, recovery, recovery_pin = files
    preceding_project = panel.project
    plan = panel.load_pilot_receipt(rehearsal, expected_sha256=rehearsal_pin, kind="rehearsal")
    response = panel.load_pilot_receipt(recovery, expected_sha256=recovery_pin, kind="recovery")
    displayed = panel.receipt_text.get("1.0", "end").casefold()
    verified = all("Integrity verified" in panel.receipt_summaries[kind].get()
                   for kind in ("rehearsal", "recovery"))
    if ("rehearsal receipt" not in displayed or "recovery receipt" not in displayed
            or not verified or panel.receipt_text.cget("state") != "disabled"
            or panel.project is not preceding_project or plan["action_authority"] or response["action_authority"]):
        raise RuntimeError("The receipt view did not show both read-only proposals.")
    return {"rehearsal": plan["status"], "recovery": response["status"], "action_authority": False}


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
    if argv and argv[0] == "--planning-worker":
        if len(argv) != 4:
            raise SystemExit(2)
        from .planning_jobs import worker
        worker(*argv[1:])
        return
    if argv and argv[0] == "--future-reader-worker":
        if len(argv) != 5 or argv[1] != "_worker":
            raise SystemExit(2)
        from future_reader.live_failover import main as worker_main

        raise SystemExit(worker_main(argv[1:]))
    parser = argparse.ArgumentParser(prog="austins-algorithm-workbench", description=__doc__)
    parser.add_argument("--version", action="version", version=DESKTOP_VERSION)
    parser.add_argument("--smoke", type=Path, metavar="DIRECTORY", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-receipts", nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.smoke_receipts and args.smoke is None:
        parser.error("--smoke-receipts requires --smoke")
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
        if args.smoke is not None:
            root.withdraw()
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
            status['planning'] = _planning_smoke(app,args.smoke)
            app.show_page('Security Lab')
            panel=app.security
            from .planning_jobs import JobStore,verify_export
            panel.store.close();panel.store=JobStore(args.smoke/'security-jobs')
            panel.start()
            import time
            deadline=time.monotonic()+30
            while panel.active() and time.monotonic()<deadline:
                root.update();time.sleep(.02)
            record=panel.store.poll(panel.job_id)
            if record['state']!='COMPLETED' or record['result']['report']['status']!='MODEL_CHECKS_PASSED':
                raise RuntimeError('Security Lab worker did not complete its named synthetic checks')
            security_path=args.smoke/'security.security.json';security_export=args.smoke/'security.zip'
            panel.save(security_path);panel.export(security_export);panel.open(security_path)
            verify_export(security_export)
            status['security_lab']={'named_model_checks':'PASS','job_save_open_export':'PASS','quantum_advantage':'NOT_DEMONSTRATED','network_used':False}
            status["focus"] = _focus_smoke(app, args.smoke)
            status["decision_review"] = _decision_smoke(app, args.smoke)
            if args.smoke_receipts:
                status["decision_review"]["pilot_receipts"] = _receipt_smoke(app.decision_review, args.smoke_receipts)
            if root.state() != "withdrawn":
                raise RuntimeError("The automated smoke check displayed the Workbench window.")
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
