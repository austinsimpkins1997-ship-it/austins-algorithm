import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


class SecurityPanelTests(unittest.TestCase):
    def test_hidden_workbench_job_save_load_export_and_changed_input_hold(self):
        try:
            import tkinter as tk
            root=tk.Tk();root.withdraw()
        except Exception as exc:self.skipTest('Tk unavailable: '+str(exc))
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{'LOCALAPPDATA':directory,'APPDATA':directory}):
            from austins_algorithm.workbench import Workbench
            app=Workbench(root)
            try:
                app.show_page('Security Lab');panel=app.security
                panel.start();deadline=time.monotonic()+20
                while panel.active() and time.monotonic()<deadline:
                    root.update();time.sleep(.02)
                panel._poll()
                self.assertIn('MODEL_CHECKS_PASSED',panel.result.get('1.0','end'))
                path=Path(directory)/'lab.security.json';archive=Path(directory)/'evidence.zip'
                self.assertTrue(app.save(path));app.export(archive);self.assertTrue(archive.is_file())
                panel.editor.insert('end',' ')
                # Whitespace alone changes no model; a real policy change holds the current result.
                raw=panel.editor.get('1.0','end').replace('"max_failures": 3','"max_failures": 4')
                panel.editor.delete('1.0','end');panel.editor.insert('1.0',raw)
                with self.assertRaises(ValueError):panel.export(Path(directory)/'changed.zip')
                panel.dirty=False
                self.assertTrue(app.open(path))
                self.assertEqual(root.state(),'withdrawn')
            finally:
                app.security.close();app.planning.close();app.future_reader.close();root.destroy()
                import gc
                app=None;panel=None;root=None;gc.collect()
