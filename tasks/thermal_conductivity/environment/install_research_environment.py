#!/usr/bin/env python3
"""Build the private research Python without network access or SDK modification."""
import argparse,json,os,platform,shutil,subprocess,sys,tempfile
from pathlib import Path
from assemble_wheels import assemble


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,default=Path(__file__).resolve().parent);ap.add_argument('--prefix',type=Path,default=Path('/opt/sie-research'));a=ap.parse_args()
    if platform.system()!='Linux' or platform.machine() not in {'x86_64','AMD64'}:
        raise RuntimeError('Research wheels require Linux x86_64; use an amd64 operator base image.')
    if sys.version_info[:2]!=(3,11):
        raise RuntimeError('Research wheels require CPython 3.11 from the operator base image.')
    libc,version=platform.libc_ver()
    if libc!='glibc' or tuple(map(int,version.split('.')[:2]))<(2,28):
        raise RuntimeError(f'Research wheels require glibc >= 2.28, detected {libc} {version}.')
    if a.prefix.exists():raise RuntimeError(f'Refusing to overwrite an existing prefix: {a.prefix}')
    source=a.source.resolve();manifest=json.loads((source/'wheel_manifest.json').read_text())
    env=dict(os.environ)
    for key in ['PYTHONPATH','PYTHONHOME','PIP_INDEX_URL','PIP_EXTRA_INDEX_URL','PIP_TARGET','PIP_PREFIX','PIP_USER']:env.pop(key,None)
    env.update({'PIP_CONFIG_FILE':'/dev/null','PIP_NO_INDEX':'1','PIP_DISABLE_PIP_VERSION_CHECK':'1','PIP_NO_CACHE_DIR':'1','PYTHONNOUSERSITE':'1'})
    with tempfile.TemporaryDirectory(prefix='sie-research-wheels-') as temporary:
        wheels=Path(temporary);assembly=assemble(source,wheels)
        subprocess.run([sys.executable,'-m','venv',str(a.prefix)],check=True,env=env)
        python=a.prefix/'bin/python'
        base=[str(python),'-m','pip','install','--no-index','--no-cache-dir','--only-binary=:all:','--require-hashes','--find-links',str(wheels)]
        for lock in ['requirements-bootstrap.lock','requirements-research.lock']:
            subprocess.run(base+['-r',str(source/lock)],check=True,env=env)
        subprocess.run([str(python),'-m','pip','check'],check=True,env=env)
        validation=r'''
import importlib.metadata as m,json,platform,sys
import torch,numpy,scipy,pymatgen,ase,phonopy,phono3py,spglib,symfc
import phono3py._phono3py
from chgnet.model.model import CHGNet
from pathlib import Path
manifest=json.loads(Path(sys.argv[1]).read_text())
for w in manifest['wheels']:
    if m.version(w['name'])!=w['version']:
        raise RuntimeError('Installed version mismatch: '+w['name'])
assert torch.version.cuda is None, 'Expected the isolated CPU-only torch wheel'
assert torch.tensor([1.,2.]).sum().item()==3.
assert numpy.__version__=='1.26.4'
assert spglib.get_symmetry_dataset((numpy.eye(3),[[0,0,0]],[1])).number==221
report={'status':'private_linux_research_venv_installed_and_native_imports_passed','python':platform.python_version(),'platform':platform.platform(),'architecture':platform.machine(),'torch':torch.__version__,'torch_cuda':torch.version.cuda,'numpy':numpy.__version__,'distributions':len(manifest['wheels']),'site_packages_isolated':not any('system-site-packages = true' in line for line in (Path(sys.prefix)/'pyvenv.cfg').read_text().splitlines()),'note':'This is a build-time dependency check. A full scientific CHGNet-to-thermal-conductivity run must use the model weights and geometry from task input.'}
assert report['site_packages_isolated']
(Path(sys.prefix)/'ENVIRONMENT_VERIFICATION.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
'''
        subprocess.run([str(python),'-c',validation,str(source/'wheel_manifest.json')],check=True,env=env)
    audit=a.prefix/'share/sie-research';audit.mkdir(parents=True)
    for name in ['wheel_manifest.json','requirements-bootstrap.lock','requirements-research.lock','dependency_validation.json','native_library_validation.json','offline_resolution_validation.json']:
        shutil.copyfile(source/name,audit/name)
    (audit/'artifact_assembly_verification.json').write_text(json.dumps(assembly,indent=2)+'\n')
    print('Research runtime installed at '+str(a.prefix)+'. The SDK/default Python and PATH were not changed.')
if __name__=='__main__':main()
