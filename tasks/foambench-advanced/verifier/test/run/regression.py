"""Run only on D: parser, mesh correspondence, reference reads and cavity smoke."""
import json,sys,tarfile,shutil,struct,traceback,subprocess,time
from pathlib import Path
sys.path.insert(0,'/verifier/test/run')
import numpy as np
import pyvista as pv
import foamfile,nmse,runner,submission
import main as verifier
out=Path('/output');work=out/'probe-work';work.mkdir()
result={'status':'running','checks':[],'reference_self_nmse':{}}
def save(): (out/'regression.json').write_text(json.dumps(result,indent=2))
def check(name, fn):
    start=time.monotonic()
    try:
        fn();result['checks'].append({'name':name,'passed':True,'seconds':time.monotonic()-start})
    except Exception as error:
        result['checks'].append({'name':name,'passed':False,'error':str(error),'traceback':traceback.format_exc()})
    save()
def expect_error(fn,kind=ValueError):
    try: fn()
    except kind:return
    raise AssertionError('expected rejection')
def header(fmt='ascii',cls='volScalarField',arch='LSB;label=32;scalar=64'):
    return f'FoamFile {{ version 2.0; format {fmt}; arch "{arch}"; class {cls}; object p; }}\ndimensions [0 2 -2 0 0 0 0];\n'.encode()
def parser_checks():
    import gzip
    path=work/'field'
    valid=header()+b'internalField nonuniform List<scalar> 2(1 2); boundaryField {}'
    path.write_bytes(valid);assert foamfile.field_is_finite(path)
    zipped=work/'field.gz';zipped.write_bytes(gzip.compress(valid));assert foamfile.field_is_finite(zipped)
    for bad in [valid.replace(b'2(1 2)',b'3(1 2)'),valid[:-1],valid.replace(b'1 2',b'1 nan'),valid.replace(b'2(1 2)',b'broken')]:
        path.write_bytes(bad);expect_error(lambda:foamfile.field_is_finite(path),foamfile.FieldError)
    for bits,fmt in [(32,'<ff'),(64,'<dd')]:
        binary=header('binary',arch=f'LSB;label=32;scalar={bits}')+b'internalField nonuniform List<scalar> 2('+struct.pack(fmt,1.,2.)+b'); boundaryField {}'
        path.write_bytes(binary);assert foamfile.field_is_finite(path)
        path.write_bytes(binary.replace(struct.pack(fmt,1.,2.),struct.pack(fmt,1.,float('nan'))));assert not foamfile.field_is_finite(path)
    zipped.write_bytes(b'badgzip');expect_error(lambda:foamfile.is_field(zipped),foamfile.FieldError)
    path.write_bytes(b'internalField uniform 0; boundaryField {}');expect_error(lambda:foamfile.is_field(path),foamfile.FieldError)
check('ASCII binary32 binary64 gzip corruption and finiteness',parser_checks)
def field_checks():
    c=work/'required';(c/'0').mkdir(parents=True);(c/'1').mkdir()
    (c/'1/p').write_bytes(header()+b'internalField uniform 0; boundaryField {}')
    assert 'U' in verifier.check_fields(c,{'0':0},['U','p'])
    (c/'1/U').write_bytes(header(cls='volVectorField')+b'internalField uniform (0 0 0); boundaryField {}')
    assert verifier.check_fields(c,{'0':0},['U','p']) is None
    (c/'1/aux.gz').write_bytes(b'broken');assert 'unreadable' in verifier.check_fields(c,{'0':0},['U','p'])
check('missing velocity fails and corrupt additional field cannot be skipped',field_checks)
def submission_checks():
    p=work/'cases.json';p.write_text('{"x":{"0/U":"\\ud800"}}');expect_error(lambda:submission.load_submission(p,['x']),submission.SubmissionError)
    p.write_text('['*2000+']'*2000);expect_error(lambda:submission.load_submission(p,['x']),submission.SubmissionError)
check('invalid Unicode and deep JSON become invalid submissions',submission_checks)
def execution_failure_check():
    from unittest.mock import patch
    class Context:
        def log(self, value): pass
    folder=work/'failure';folder.mkdir()
    with patch.object(runner,'read_control_entry',return_value='pisoFoam'), patch.object(runner,'run_case',return_value=(1,False)), patch.object(verifier,'compute_nmse',side_effect=AssertionError('must skip NMSE')):
        value=verifier.evaluate_case('failed',{'system/controlDict':'x'},{'solver':'pisoFoam','wall_clock_limit_seconds':1,'required_fields':['U','p']},folder,Context())
        assert value==(0,9999.,0.)
    assert (folder/'logs/failed.summary.json').is_file()
    scratch=verifier.scratch_directory(folder/'output/result.json')
    assert scratch.parent==folder/'output'
    shutil.rmtree(scratch)
    verifier.retain_logs(folder/'logs',folder/'retained',limit=20)
    assert (folder/'retained/failed.summary.json').stat().st_size<=37
check('failed execution skips NMSE and persistent scratch bounded logs',execution_failure_check)

def mesh(nx=5):
    m=pv.ImageData(dimensions=(nx,4,2),spacing=(1/(nx-1),1/3,1)).cast_to_unstructured_grid()
    c=m.cell_centers().points
    m.cell_data['U']=np.column_stack((1+c[:,0],2+c[:,1],np.ones(m.n_cells)))
    m.cell_data['p']=1+c[:,0]*2
    return m

def mesh_checks():
    ref=mesh();value,info=nmse.compare_meshes(ref,ref,['U','p']);assert value==0
    perm=np.random.default_rng(35).permutation(ref.n_cells)
    # VTK extraction can sort IDs; construct connectivity explicitly to guarantee a permutation.
    rows=ref.cells.reshape(ref.n_cells,-1)
    other=pv.UnstructuredGrid(rows[perm].ravel(),ref.celltypes[perm],ref.points.copy())
    for field in ['U','p']:other.cell_data[field]=ref.cell_data[field][perm]
    assert not np.array_equal(ref.cell_centers().points,other.cell_centers().points)
    value,info=nmse.compare_meshes(ref,other,['U','p']);assert value==0 and info['mapping']=='matched_cell_centers'
    other.cell_data['U']=ref.cell_data['U'];value,_=nmse.compare_meshes(ref,other,['U','p']);assert value>0
    missing=ref.copy();del missing.cell_data['U'];expect_error(lambda:nmse.compare_meshes(ref,missing,['U','p']))
    for candidate in (mesh(6),mesh()):
        if candidate.n_cells==ref.n_cells:
            candidate.points[4:8,0] += 0.02
        value,info=nmse.compare_meshes(ref,candidate,['U','p']);assert np.isfinite(value) and info['mapping']=='spatial_interpolation'
    assert nmse.calculate_nmse(np.zeros(2),np.zeros(2))==0
    assert nmse.calculate_nmse(np.zeros(2),np.ones(2))==9999
check('cell permutation spatial interpolation missing fields zero denominator',mesh_checks)

def references():
    specs=verifier.load_spec()
    for name,spec in specs.items():
        target=work/'references'/name;target.mkdir(parents=True)
        with tarfile.open(verifier.DATA/'reference'/f'{name}.tar.gz') as archive: archive.extractall(target,filter='data')
        m,t=nmse.read_case(target)
        value,info=nmse.compare_meshes(m,m,spec['required_fields']);assert value==0,(name,value)
        for field in spec['required_fields']:
            candidates=[target/spec['reference_final_time']/field,target/spec['reference_final_time']/(field+'.gz')]
            path=next(p for p in candidates if p.is_file());assert foamfile.field_is_finite(path),(name,field)
        result['reference_self_nmse'][name]={'nmse':value,'time':t,'cells':m.n_cells};save()
        shutil.rmtree(target)
check('all 13 packaged reference fields parse and self-score zero',references)
def smoke():
    case=work/'cavity';home=work/'home'
    script='source "$1"; cp -r "$FOAM_TUTORIALS/incompressible/icoFoam/cavity/cavity" "$2"; cd "$2"; foamDictionary -entry writeFormat -set binary system/controlDict > /dev/null; blockMesh > log.blockMesh && icoFoam > log.icoFoam'
    code,timed=runner.run_script(script,[str(runner.OPENFOAM_BASHRC),str(case)],work,home,120,out/'cavity.log')
    assert code==0 and not timed
    reason=verifier.check_fields(case,{'0':0},['U','p']);assert reason is None,reason
    m,t=nmse.read_case(case);value,_=nmse.compare_meshes(m,m,['U','p']);assert value==0 and t==0.5
    # Exercise the actual child-process reference/candidate protocol.
    process=subprocess.run([sys.executable,'-B','/verifier/test/run/nmse.py',str(case),str(case),'U,p'],capture_output=True,text=True,timeout=120)
    assert process.returncode==0,process.stderr
    assert json.loads(process.stdout)['nmse']==0
    (case/'0.5/U').unlink()
    reason=verifier.check_fields(case,{'0':0},['U','p']);assert 'U' in reason
    process=subprocess.run([sys.executable,'-B','/verifier/test/run/nmse.py',str(case),str(case),'U,p'],capture_output=True,text=True,timeout=120)
    assert process.returncode==2 # broken reference must not become a candidate zero
check('real binary OpenFOAM cavity execution field checks and scorer CLI',smoke)
result['status']='passed' if all(c['passed'] for c in result['checks']) else 'failed'
save();shutil.rmtree(work)
print(json.dumps(result));sys.exit(0 if result['status']=='passed' else 1)
