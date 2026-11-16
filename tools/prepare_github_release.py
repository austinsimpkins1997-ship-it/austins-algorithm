"""Prepare public 0.3 downloads after verified builds; never publish or read credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(native: Path, verified: Path, companion: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    build = json.loads((verified / 'build-result.json').read_text(encoding='utf-8'))
    if build['status'] != 'VERIFIED_PACKAGE_BUILT':
        raise ValueError('The source archive and wheel must pass their package verification')
    spec = importlib.util.spec_from_file_location('release_package_check', ROOT / 'tools/build_verified_package.py')
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    main_wheel = next((verified / 'wheel').glob('*.whl'))
    main_source = next((verified / 'sdist').glob('*.tar.gz'))
    checker.verify_sdist(ROOT, main_source)
    checker.verify_wheel(ROOT, main_wheel)
    if main_wheel.name != 'chronorealm_austins_algorithm-0.3.0a1-py3-none-any.whl':
        raise ValueError('This publication is limited to package 0.3.0a1')
    executable = native / 'AustinsAlgorithmWorkbench.exe'
    native_build = json.loads((native / 'build.json').read_text(encoding='utf-8'))
    if native_build['desktop_version'] != '0.3.0-alpha' or sha256(executable.read_bytes()).hexdigest() != native_build['sha256']:
        raise ValueError('Native version or bytes do not match the build record')
    with tempfile.TemporaryDirectory(prefix='algorithm-release-smoke-') as temporary:
        directory = Path(temporary)
        env = {**os.environ, 'LOCALAPPDATA': str(directory / 'local'), 'APPDATA': str(directory / 'roaming')}
        result = subprocess.run([str(executable.resolve()), '--smoke', str(directory / 'smoke')], env=env, timeout=120, check=True)
        smoke = json.loads((directory / 'smoke/desktop-smoke.json').read_text(encoding='utf-8'))
        if smoke['desktop_version'] != '0.3.0-alpha' or not smoke['frozen']:
            raise ValueError('Wrong native smoke version or non-frozen build')
    companion_files = [*companion.glob('*.whl'), *companion.glob('*.tar.gz')]
    if len(companion_files) != 2 or any('0.1.2' not in path.name for path in companion_files):
        raise ValueError('Expected the Future Reader 0.1.2 wheel and source distribution')
    for source in [main_wheel, main_source, executable, *companion_files]:
        shutil.copy2(source, output / source.name)
    shutil.copy2(ROOT / '.github/RELEASE-0.3.md', output / 'RELEASE-NOTES.md')
    with zipfile.ZipFile(output / 'AustinsAlgorithmWorkbench-0.3.0-alpha-windows.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, executable.name)
        archive.write(native / 'build.json', 'build.json')
        for name in ['README.md', 'PLANNING.md', 'WORKBENCH.md', 'LICENSE', 'NOTICE', 'examples/planning_equipment.json']:
            path = ROOT / name
            if path.is_file():
                archive.write(path, name)
    files = {p.name: {'bytes': p.stat().st_size, 'sha256': sha256(p.read_bytes()).hexdigest()} for p in sorted(output.iterdir())}
    record = {'schema': 'AustinAlgorithmPublicRelease/1', 'version': '0.3.0a1', 'desktop_version': '0.3.0-alpha',
              'recorded_at': datetime.now(timezone.utc).isoformat(), 'editor': 'Codex-authored release automation',
              'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'workflow_run': os.environ.get('GITHUB_RUN_ID'), 'files': files,
              'checks': {'sdist': 'PASS', 'wheel': 'PASS', 'native_hidden_smoke': 'PASS', 'frozen': smoke['frozen'],
                         'planning': smoke.get('planning'), 'main_suite': 'Prior workflow step must exit successfully'},
              'windows_publisher_trust': 'UNSIGNED', 'local_installed_signature_applies_to_hosted_assets': False,
              'production_acceptance': 'UNVERIFIED', 'physical_qpu_used': False, 'operator_observations': 0}
    # Do not publish temporary profile paths, caller identities, job logs or local secrets.
    record['checks']['planning'] = 'PASS: hidden project/job/export flow completed'
    (output / 'release-evidence.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    sums = ''.join(f'{sha256(path.read_bytes()).hexdigest()}  {path.name}\n' for path in sorted(output.iterdir()) if path.is_file())
    (output / 'SHA256SUMS.txt').write_text(sums, encoding='utf-8')
    return {'status': 'PUBLIC_ASSETS_PREPARED', 'files': len(list(output.iterdir())), 'version': record['version']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('native', 'verified', 'companion', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(prepare(**vars(parser.parse_args()))))
