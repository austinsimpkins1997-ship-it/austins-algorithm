"""Keyboard-accessible planning editor and shared job/result overview."""
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .planning import API_VERSION, canonical, overview, problem_from_document, example_problem, format_overview
from .planning_cli import read_problem
from .planning_jobs import JobStore, export_job


class PlanningPanel(ttk.Frame):
    def __init__(self,parent,*,store=None):
        super().__init__(parent)
        directory=Path(os.environ.get('LOCALAPPDATA',Path.home()))/'ChronoRealm/AustinWorkbench/planning-jobs'
        self.store=store or JobStore(directory)
        self.job_id=None
        self.after_id=None
        self.title=ttk.Label(self,text='Planning - choose a plan, verify it, keep the evidence',font=('Segoe UI',16,'bold'))
        self.title.pack(anchor='w',padx=12,pady=8)
        ttk.Label(self,text='Classical optimization: up to 128 Boolean choices. Each rule lists alternatives; every rule must pass.\nCosts are nonnegative integers. Positive literals select a choice; negative literals exclude it. No quantum hardware is used.',wraplength=850).pack(anchor='w',padx=12)
        bar=ttk.Frame(self);bar.pack(fill='x',padx=12,pady=8)
        for label,command in [('New example',self.new),('Open project',self.open),('Save project',self.save),('Start',self.start),('Stop',self.stop),('Resume',self.resume),('Recover',self.recover),('Export all',self.export)]:
            ttk.Button(bar,text=label,command=lambda f=command:self.safe(f)).pack(side='left',padx=2)
        controls=ttk.Frame(self);controls.pack(fill='x',padx=12)
        self.nodes=tk.StringVar(self,value='100000');self.seconds=tk.StringVar(self,value='30')
        self.backend=tk.StringVar(self,value='branch-bound')
        for label,var in [('Nodes per attempt',self.nodes),('Seconds per attempt',self.seconds)]:
            ttk.Label(controls,text=label).pack(side='left');ttk.Entry(controls,textvariable=var,width=10).pack(side='left',padx=5)
        ttk.Combobox(controls,textvariable=self.backend,values=('branch-bound','scipy-milp'),state='readonly',width=17).pack(side='left')
        ttk.Label(controls,text='SciPy is optional and separately installed; the portable EXE uses branch-bound.').pack(side='left',padx=4)
        self.summary=tk.StringVar(self,value='Ready. Edit the example or open a project, then Start.')
        ttk.Label(self,textvariable=self.summary,wraplength=900,justify='left').pack(fill='x',padx=12,pady=8)
        panes=ttk.Panedwindow(self,orient='horizontal');panes.pack(expand=True,fill='both',padx=12,pady=6)
        left=ttk.Frame(panes);right=ttk.Frame(panes);panes.add(left,weight=1);panes.add(right,weight=1)
        ttk.Label(left,text='Project input (JSON) - edit choice names, costs and CNF rules').pack(anchor='w')
        self.editor=tk.Text(left,wrap='none',undo=True,width=40,height=18);self.editor.pack(expand=True,fill='both')
        ttk.Label(right,text='What happened? The same summary is included in CLI and exports.').pack(anchor='w')
        self.output=tk.Text(right,wrap='word',state='disabled',width=45,height=18);self.output.pack(expand=True,fill='both')
        # List persisted jobs explicitly; reopening never silently executes a job.
        bottom=ttk.Frame(self);bottom.pack(fill='x',padx=12,pady=6)
        self.selected_job=tk.StringVar(self)
        self.jobs=ttk.Combobox(bottom,textvariable=self.selected_job,state='readonly',width=35);self.jobs.pack(side='left')
        ttk.Button(bottom,text='Open saved job',command=lambda:self.safe(self.open_job)).pack(side='left')
        ttk.Button(bottom,text='Refresh jobs',command=lambda:self.safe(self.refresh_jobs)).pack(side='left')
        ttk.Button(bottom,text='Delete selected job',command=lambda:self.safe(self.delete_job)).pack(side='left')
        self.new();self.refresh_jobs()

    def safe(self,function):
        try:
            return function()
        except (ValueError,OSError,RuntimeError,ImportError) as exc:
            self.summary.set(f'Action held: {exc}')
            return None

    def input(self):
        raw=self.editor.get('1.0','end')
        if len(raw.encode('utf-8'))>1000000:raise ValueError('Planning input exceeds 1 MB')
        from .constraints import _unique_object
        value=json.loads(raw,object_pairs_hook=_unique_object)
        return problem_from_document(value)

    def limits(self):
        return {'nodes':int(self.nodes.get()),'seconds':float(self.seconds.get())}

    def active(self):
        return self.job_id and self.store.poll(self.job_id)['state'] in ('STARTING','RUNNING')

    def new(self):
        if self.active(): raise ValueError('Stop the current job before replacing the editor')
        self.job_id=None;self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(example_problem(),indent=2))

    def start(self):
        if self.active(): raise ValueError('One Planning job can run from this page at a time')
        record=self.store.create(self.input(),budget=self.limits(),backend=self.backend.get())
        self.job_id=record['id'];self.store.start(self.job_id);self.refresh_jobs();self.poll()

    def poll(self):
        if self.after_id:
            self.after_cancel(self.after_id);self.after_id=None
        if not self.job_id:return
        record=self.store.poll(self.job_id)
        view=overview(record['problem'],record['result'],state=record['state'],error=record['error'])
        self.summary.set(f"{view['stage']} | {view['status']} | {view['model']}\n{view['next_action']}")
        self.output.configure(state='normal');self.output.delete('1.0','end')
        self.output.insert('1.0',format_overview(view));self.output.configure(state='disabled')
        if record['state'] in ('STARTING','RUNNING'):
            self.after_id=self.after(100,lambda:self.safe(self.poll))
        return view

    def stop(self):
        if self.job_id:self.store.stop(self.job_id);self.poll()

    def resume(self):
        if not self.job_id:raise ValueError('Open a saved job first')
        record=self.store.read(self.job_id)
        if self.input()!=record['problem']:raise ValueError('Inputs changed: Start a new job rather than resuming stale evidence')
        self.store.start(self.job_id,resume=True,budget=self.limits());self.poll()

    def recover(self):
        if self.job_id:self.store.recover(self.job_id);self.poll()

    def refresh_jobs(self):
        self.jobs['values']=[p.parent.name for p in sorted(self.store.root.glob('*/job.json'))]

    def open_job(self):
        if self.active():raise ValueError('Stop the active job before switching')
        record=self.store.poll(self.selected_job.get());self.job_id=record['id']
        self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(record['problem'],indent=2))
        self.nodes.set(str(record['budget']['nodes']));self.seconds.set(str(record['budget']['seconds']));self.backend.set(record['backend']);self.poll()

    def delete_job(self):
        from tkinter import messagebox
        job_id=self.selected_job.get()
        if messagebox.askyesno('Delete saved job','Delete this job and its local evidence? Export first if you need a copy.',parent=self):
            self.store.delete(job_id)
            if self.job_id==job_id:self.job_id=None
            self.refresh_jobs()

    def save(self,path=None):
        path=path or filedialog.asksaveasfilename(parent=self,defaultextension='.planning.json')
        if path:
            from .planning import budget_from_document
            value={'schema_version':API_VERSION,'problem':self.input(),'budget':budget_from_document(self.limits()),'backend':self.backend.get()}
            from .planning_jobs import _write
            _write(Path(path),value);self.summary.set('Project saved. A project stores inputs; job checkpoints remain in the job list.')
        return path

    def open(self,path=None):
        if self.active():raise ValueError('Stop the active job before opening another project')
        path=path or filedialog.askopenfilename(parent=self,filetypes=[('Planning or legacy weighted input','*.json')])
        if path:
            problem,budget,backend=read_problem(Path(path));self.job_id=None
            self.editor.delete('1.0','end');self.editor.insert('1.0',json.dumps(problem,indent=2))
            if budget:self.nodes.set(str(budget['nodes']));self.seconds.set(str(budget['seconds']))
            if backend:self.backend.set(backend)
        return path

    def export(self,path=None):
        if not self.job_id:raise ValueError('Run a job before exporting its evidence')
        path=path or filedialog.asksaveasfilename(parent=self,defaultextension='.zip')
        if path:
            if self.input()!=self.store.read(self.job_id)['problem']:
                raise ValueError('Editor differs from this job. Save edits separately or open the recorded job before export.')
            result=export_job(self.store,self.job_id,path);self.summary.set('Exported inputs, result, checkpoint, attempts, logs, overview and fresh replay code.');return result

    def close(self):
        if self.after_id:self.after_cancel(self.after_id);self.after_id=None
        self.store.close()

    def destroy(self):
        # Release Tcl variables on the UI thread while the interpreter is alive.
        # Leaving widget cycles for a later simulator thread's GC can crash Tcl.
        self.close()
        super().destroy()
        for name in ('nodes','seconds','backend','summary','selected_job'):
            setattr(self,name,None)

    def explain(self):
        from tkinter import messagebox
        view=self.poll() if self.job_id else overview(self.input())
        messagebox.showinfo('Local Planning Assistant',
            f"{view['model']}\nStatus: {view['status']}\nOptimality: {view['optimality']}\n\n{view['next_action']}\n\n"
            'Every CNF rule is mandatory. Start creates a new job. Resume requires unchanged inputs and a trusted local checkpoint. '
            'Stop retains committed work. Recover only repairs an inactive interrupted job. Export includes fresh replay code. '
            'This assistant uses local rules; it does not call an AI model or grant approval.',parent=self)
