#!/usr/bin/env python3
"""Validate every vendored artifact and reconstruct immutable wheel files offline."""
import argparse,hashlib,json,os,shutil
from pathlib import Path


def safe_file(root, relative):
    relative=Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError(f'Unsafe artifact path: {relative}')
    p=(root/relative).resolve()
    p.relative_to(root.resolve())
    if not p.is_file():raise FileNotFoundError(relative)
    return p


def assemble(root, output=None):
    manifest=json.loads((root/'wheel_manifest.json').read_text())
    if output is not None:
        output.mkdir(parents=True,exist_ok=True)
    checked=[]
    for wheel in manifest['wheels']:
        name=wheel['filename']
        if Path(name).name!=name or not name.endswith('.whl'):raise ValueError(name)
        total=hashlib.sha256();size=0;parts=[]
        for artifact in wheel['artifacts']:
            p=safe_file(root,artifact['path']);part_hash=hashlib.sha256();part_bytes=0
            with p.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):
                    part_hash.update(block);total.update(block);part_bytes+=len(block);size+=len(block)
            if part_bytes!=artifact['bytes'] or part_hash.hexdigest()!=artifact['sha256']:
                raise ValueError(f'Corrupt artifact: {artifact["path"]}')
            parts.append(p)
        if size!=wheel['bytes'] or total.hexdigest()!=wheel['sha256']:
            raise ValueError(f'Corrupt assembled wheel: {name}')
        if output is not None:
            target=output/name
            if target.exists():raise FileExistsError(target)
            if len(parts)==1:
                try:os.link(parts[0],target)
                except OSError:shutil.copyfile(parts[0],target)
            else:
                tmp=target.with_suffix(target.suffix+'.assembling')
                with tmp.open('wb') as out:
                    for p in parts:
                        with p.open('rb') as f:shutil.copyfileobj(f,out,1024*1024)
                h=hashlib.sha256()
                with tmp.open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                if tmp.stat().st_size!=size or h.hexdigest()!=wheel['sha256']:
                    tmp.unlink();raise ValueError(f'Reconstruction failed: {name}')
                tmp.rename(target)
        checked.append({'filename':name,'bytes':size,'sha256':wheel['sha256']})
    return {'status':'all_artifacts_and_complete_wheels_verified','wheel_count':len(checked),'wheels':checked}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--source',type=Path,default=Path(__file__).resolve().parent);ap.add_argument('--output',type=Path);ap.add_argument('--report',type=Path);a=ap.parse_args()
    report=assemble(a.source.resolve(),a.output)
    if a.report:a.report.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'wheel_count':report['wheel_count']}))
if __name__=='__main__':main()
