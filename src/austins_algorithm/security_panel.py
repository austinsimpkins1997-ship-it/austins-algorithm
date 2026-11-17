"""Security Lab GUI for bounded synthetic jobs and visible verification traces."""
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox

from . import security_rehearsal as lab
from .planning_jobs import JobStore,export_job,_write
from .security_cli import read_document


class SecurityPanel(ttk.Frame):
    def __init__(self,parent):
        super().__init__(parent)
        self.store=JobStore(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'ChronoRealm/AustinWorkbench/security-jobs')
        self.job_id=None;self.after_id=None;self.dirty=False
        ttk.Label(self,text='Security Lab — inspect the model, test a policy, verify the sequence',font=('Segoe UI',15,'bold')).pack(anchor='w',padx=10,pady=8)
        ttk.Label(self,text='Synthetic accounts and a public three-bit teaching oracle only. No passwords, URLs, network calls or physical QPU.\nPASS describes the named model checks, not production security or a quantum cracking advantage.',wraplength=850).pack(anchor='w',padx=10)
        bar=ttk.Frame(self);bar.pack(fill='x',padx=10,pady=8)
        for label,callback in [('New',self.new),('Open',self.open),('Save',self.save),('Run lab',self.start),('Stop',self.stop),('Export',self.export),('Circuit QASM',self.circuit),('Help',self.explain)]:
            ttk.Button(bar,text=label,command=lambda fn=callback:self.safe(fn)).pack(side='left',padx=2)
        tabs=ttk.Notebook(self);tabs.pack(fill='both',expand=True,padx=10,pady=5)
        self.editor=tk.Text(tabs,height=12,wrap='word',undo=True);self.result=tk.Text(tabs,height=15,wrap='word')
        self.sources=tk.Text(tabs,wrap='word')
        tabs.add(self.editor,text='Inputs and policy');tabs.add(self.result,text='What happened?');tabs.add(self.sources,text='Sources and limits')
        self.tabs=tabs
        self.editor.bind('<<Modified>>',self._edited)
        source_path=Path(__file__).parent/'workbench_assets/security_sources.json'
        self.sources.insert('1.0',json.dumps(json.loads(source_path.read_text(encoding='utf-8')),indent=2,ensure_ascii=False));self.sources.configure(state='disabled')
        self.new()

    def safe(self,fn):
        try:return fn()
        except (ValueError,OSError,RuntimeError,ImportError) as exc:messagebox.showerror('Security Lab',str(exc),parent=self)

    def _edited(self,_event=None):
        if self.editor.edit_modified():self.dirty=True;self.editor.edit_modified(False)

    def confirm_discard(self):
        return not self.dirty or messagebox.askyesno('Unsaved Security Lab inputs','Discard unsaved policy edits?',parent=self)

    def active(self):
        return bool(self.job_id and self.store.poll(self.job_id)['state'] in ('STARTING','RUNNING'))

    def problem(self):
        raw=self.editor.get('1.0','end').encode('utf-8')
        if len(raw)>32768:raise ValueError('Input exceeds 32 KiB')
        # The CLI and GUI both reject duplicate keys; no source instructions are executed.
        def pairs(items):
            d={}
            for k,v in items:
                if k in d:raise ValueError('Duplicate JSON key')
                d[k]=v
            return d
        return lab.document(json.loads(raw,object_pairs_hook=pairs))

    def new(self):
        if self.active():raise ValueError('Stop the active lab before replacing its inputs')
        if not self.confirm_discard():return
        self.job_id=None;self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(lab.example_document(),indent=2))
        self.editor.edit_modified(False);self.dirty=False
        self.result.delete('1.0','end');self.result.insert('1.0','Review the named policy and fixtures, then select Run lab.\nChanging inputs creates a new pinned job; prior records remain in the private store.')

    def start(self):
        if self.active():raise ValueError('One lab job can run from this page at a time')
        job=self.store.create_task('security-rehearsal',self.problem())
        self.job_id=job['id'];self.store.start(self.job_id);self.tabs.select(self.result);self._poll()

    def _poll(self):
        self.after_id=None
        if not self.job_id:return
        job=self.store.poll(self.job_id)
        self.result.delete('1.0','end')
        if job['result']:
            report=job['result']['report'];self.result.insert('1.0',lab.summary(report)+'\n\nRecorded details:\n'+json.dumps(report,indent=2))
        else:self.result.insert('1.0',f"Job: {job['state']}\n{job.get('error') or 'Bounded worker running; Stop remains available.'}")
        if job['state'] in ('STARTING','RUNNING'):self.after_id=self.after(75,lambda:self.safe(self._poll))

    def stop(self):
        if self.job_id:self.store.stop(self.job_id)

    def save(self,path=None):
        path=path or filedialog.asksaveasfilename(parent=self,defaultextension='.security.json')
        if path:_write(Path(path),self.problem());self.dirty=False;return True
        return False

    def open(self,path=None):
        if self.active():raise ValueError('Stop the active lab before opening another input')
        if not self.confirm_discard():return False
        path=path or filedialog.askopenfilename(parent=self,filetypes=[('Security Lab inputs','*.json')])
        if not path:return False
        value=read_document(path);self.job_id=None;self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(value,indent=2));self.editor.edit_modified(False);self.dirty=False;return True

    def export(self,path=None):
        if not self.job_id:raise ValueError('Run the lab before exporting recorded results')
        if self.store.read(self.job_id)['problem']!=self.problem():raise ValueError('Inputs changed after the run; run a new pinned job before exporting this view')
        path=path or filedialog.asksaveasfilename(parent=self,defaultextension='.zip')
        if path:return export_job(self.store,self.job_id,Path(path))

    def circuit(self):
        from .search_sequence import circuit_qasm
        doc=self.problem()
        rounds=doc['sequence']['rounds'][0]
        if self.job_id:
            record=self.store.read(self.job_id)
            if record['problem']!=doc:raise ValueError('Inputs changed; run a new pinned job before using its selected sequence')
            report=(record.get('result') or {}).get('report',{})
            rounds=report.get('sequence',{}).get('selected_rounds',rounds)
        data=circuit_qasm(rounds)
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.qasm')
        if path:
            with Path(path).open('x',encoding='utf-8') as stream:stream.write(data)

    def explain(self):
        messagebox.showinfo('Security Lab guide','Run compares named synthetic fixtures and records every modeled admission. The public teaching sequence tries finite round counts, selects using ideal-model probabilities, and verifies fresh samples. It is not cryptanalysis. Circuit QASM requires the optional Qiskit extra. The source table is a representative review, not a strongest-tools ranking or a compliance certificate.',parent=self)

    def close(self):
        if self.after_id:self.after_cancel(self.after_id);self.after_id=None
        self.store.close()

    def destroy(self):
        self.close();super().destroy()
