"""Explicit offline IBM Austin-circuit route through the existing adapter contract."""
from .adapter_api import BackendCapabilities, ExecutionResult, AdapterContractError, check_compatibility


class IBMSnapshotAdapter:
    adapter_id="chronorealm.austin-ibm-snapshot"
    adapter_version="1"

    def capabilities(self):
        return BackendCapabilities(adapter_id=self.adapter_id,adapter_version=self.adapter_version,
            supported_formats=('chronorealm.austin-snapshot/1',),execution_modes=('SIMULATOR',),
            max_width=3,supports_shots=True,supports_seed=True,supports_async_jobs=False)

    def submit(self,request):
        warnings=check_compatibility(request,self.capabilities())
        p=request.workload.payload
        if not isinstance(p,dict) or set(p)!={'model'} or p['model'] not in ('manila','jakarta','torino'):
            raise AdapterContractError('Supply exactly one named offline model: manila, jakarta or torino')
        if request.workload.logical_width not in (None,3):
            raise AdapterContractError('This adapter runs the built-in three-qubit Austin circuit only')
        from .ibm_models import run_ibm_snapshot_comparison
        report=run_ibm_snapshot_comparison(model=p['model'],shots=request.shots or 64,seed=7 if request.seed is None else request.seed)
        return ExecutionResult(status='COMPLETED',execution_mode='SIMULATOR',adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,backend_id='offline-'+p['model'],result_format='application/json',
            result_payload=report,shots_requested=request.shots or 64,
            timings_seconds={'total':report['wall_seconds']},timing_sources={'total':'CLIENT_MONOTONIC'},
            warnings=(*warnings,'Dated snapshot simulation only; no provider connection or QPU job.'))

    def status(self,job_id):
        return ExecutionResult(status='NOT_SUPPORTED',execution_mode='SIMULATOR',adapter_id=self.adapter_id,
                               adapter_version=self.adapter_version,warnings=('Synchronous offline adapter; no provider job.',))

    cancel=status
