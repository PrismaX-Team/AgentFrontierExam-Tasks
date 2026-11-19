#!/usr/bin/env python3
"""Offline deterministic scoring; no submitted code is executed."""
from __future__ import annotations
import argparse
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import json
import os
from pathlib import Path

TASK_ID = 'au-ti-o-force-prediction'
PRIMARY = 'element_balanced_force_rmse'
MAX_BYTES = 32 * 1024 * 1024

class InvalidSubmission(ValueError):
    pass

def reject_constant(value):
    raise InvalidSubmission('Non-finite JSON constants are not allowed')

def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSubmission('Duplicate JSON object key')
        result[key] = value
    return result

def read_rows(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise InvalidSubmission('Required JSONL file missing, linked, or larger than 32 MiB')
    rows = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                raise InvalidSubmission('Blank JSONL records are not allowed')
            rows.append(json.loads(line, parse_int=Decimal, parse_float=Decimal,
                                   parse_constant=reject_constant, object_pairs_hook=unique_object))
    return rows

def number(value):
    if not isinstance(value, Decimal) or not value.is_finite() or abs(value) > Decimal('1e100'):
        raise InvalidSubmission('Expected a finite JSON number with absolute value at most 1e100')
    return value

def output_number(value):
    # Fixed 12 decimal places in the original numerical scale.
    return float(value.quantize(Decimal('0.000000000001'), rounding=ROUND_HALF_EVEN))

def score(submission_root, truth_path):
    truth = read_rows(truth_path)
    predictions = read_rows(submission_root / 'predictions.jsonl')
    expected = {r['id'] for r in truth}
    if len(expected) != len(truth) or not truth:
        raise RuntimeError('Invalid frozen reference data')
    if len(predictions) != len(truth):
        raise InvalidSubmission('Wrong number of prediction records')
    by_id = {}
    for row in predictions:
        if not isinstance(row, dict) or set(row) != {'id', 'energy', 'forces'}:
            raise InvalidSubmission('Each record must contain exactly id, energy, forces')
        key = row['id']
        if not isinstance(key, str) or key not in expected or key in by_id:
            raise InvalidSubmission('Unknown, duplicate, or invalid structure ID')
        by_id[key] = row
    with localcontext() as ctx:
        ctx.prec = 250
        sums = {s:Decimal(0) for s in ('Au','O','Ti')}
        counts = {s:0 for s in sums}
        energy_sum = Decimal(0)
        # Always aggregate in the frozen reference order, regardless of submission order.
        for ref in truth:
            pred = by_id[ref['id']]
            n = len(ref['species'])
            energy_error = (number(pred['energy']) - number(ref['energy'])) / n
            energy_sum += energy_error * energy_error
            if not isinstance(pred['forces'], list) or len(pred['forces']) != n:
                raise InvalidSubmission('Force array length does not match the structure')
            for s, actual, estimated in zip(ref['species'], ref['forces'], pred['forces']):
                if not isinstance(estimated, list) or len(estimated) != 3:
                    raise InvalidSubmission('Each force must contain exactly three components')
                for a, p in zip(actual, estimated):
                    delta = number(p) - number(a)
                    sums[s] += delta * delta
                    counts[s] += 1
        if any(counts[s] == 0 for s in sums):
            raise RuntimeError('Frozen reference must contain all three elements')
        balanced = (sum(sums[s]/counts[s] for s in sums)/3).sqrt()
        global_rmse = (sum(sums.values())/sum(counts.values())).sqrt()
        metrics = [
            {'name':PRIMARY,'value':output_number(balanced),'unit':'source_force_unit'},
            {'name':'all_component_force_rmse','value':output_number(global_rmse),'unit':'source_force_unit'},
            {'name':'per_atom_energy_rmse','value':output_number((energy_sum/len(truth)).sqrt()),'unit':'source_energy_unit/atom'}]
        metrics += [{'name':s+'_force_rmse','value':output_number((sums[s]/counts[s]).sqrt()),
                     'unit':'source_force_unit'} for s in sorted(sums)]
        return {'schema_version':'1.0','task_id':TASK_ID,'valid':True,'status':'valid',
                'primary_metric':{'name':PRIMARY,'value':metrics[0]['value'],'direction':'minimize'},
                'metrics':metrics,'error':None}

def atomic_json(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(tmp,path)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--submission',type=Path,required=True)
    parser.add_argument('--result',type=Path,required=True)
    args=parser.parse_args()
    # Runtime is provided by SIE. Local unit checks exercise score() directly.
    from science_innovation_exam.verify_context import VerifyContext
    context=VerifyContext.current()
    truth_path=Path(__file__).resolve().parents[1]/'data'/'reference.jsonl'
    try:
        result=score(args.submission,truth_path)
    except (InvalidSubmission,json.JSONDecodeError,UnicodeError,OSError,ArithmeticError) as exc:
        result={'schema_version':'1.0','task_id':TASK_ID,'valid':False,'status':'invalid',
                'primary_metric':{'name':PRIMARY,'value':1e100,'direction':'minimize'},
                'metrics':[{'name':PRIMARY,'value':1e100,'unit':'source_force_unit'}],'error':str(exc)}
    except Exception:
        result={'schema_version':'1.0','task_id':TASK_ID,'valid':False,'status':'invalid',
                'primary_metric':{'name':PRIMARY,'value':1e100,'direction':'minimize'},
                'metrics':[{'name':PRIMARY,'value':1e100,'unit':'source_force_unit'}],'error':'Verifier failure; no valid score produced'}
    context.log({'phase':'test','valid':result['valid']})
    atomic_json(args.result.resolve(),result)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
