"""Exercise the hidden native UI against the public API and exported evidence."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))


class PlanningPanelTests(unittest.TestCase):
    def setUp(self):
        try:
            import tkinter as tk
            self.root=tk.Tk();self.root.withdraw()
        except Exception as exc:
            self.skipTest('Tk display unavailable: '+str(exc))
        self.addCleanup(self.cleanup_ui)
        temporary=TemporaryDirectory(dir=ROOT/'.build',prefix='planning-ui-');self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)
        from austins_algorithm.planning_jobs import JobStore
        from austins_algorithm.planning_panel import PlanningPanel
        self.panel=PlanningPanel(self.root,store=JobStore(self.directory/'jobs'))
        self.addCleanup(self.panel.close)

    def cleanup_ui(self):
        import gc
        self.panel.destroy();self.panel=None
        self.root.destroy();self.root=None
        gc.collect()

    def test_save_open_managed_run_readable_summary_and_full_export(self):
        from austins_algorithm.planning_api import run_request
        from austins_algorithm.planning_jobs import verify_export
        p=self.panel.input();expected=run_request({'schema_version':'chronorealm.planning/1','problem':p,'budget':{'nodes':10000,'seconds':3},'backend':'branch-bound'})
        path=self.directory/'project.json';self.panel.save(path);self.panel.open(path)
        self.assertEqual(self.panel.input(),p)
        self.panel.start();deadline=time.monotonic()+10
        while self.panel.active() and time.monotonic()<deadline:self.root.update();time.sleep(.02)
        view=self.panel.poll()
        self.assertEqual(view['best_cost'],expected['cost'])
        self.assertIn('Best verified cost:',self.panel.output.get('1.0','end'))
        self.assertEqual(self.root.state(),'withdrawn')
        archive=self.directory/'all.zip';self.panel.export(archive)
        self.assertEqual(verify_export(archive)['status'],'PLANNING_EXPORT_VERIFIED')
        import zipfile
        from austins_algorithm.planning_cli import read_problem
        with zipfile.ZipFile(archive) as z:
            exported=self.directory/'export-project.json';exported.write_bytes(z.read('project.json'))
        self.assertEqual(read_problem(exported)[0],p)
