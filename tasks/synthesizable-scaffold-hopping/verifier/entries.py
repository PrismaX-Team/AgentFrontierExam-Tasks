"""Framework-facing entries. VerifyContext is supplied by SIE, not this task."""
import argparse
import json
import os
from pathlib import Path

from configuration import load_runtime, BYTE_CAPS, REQUEST_BYTES
from artifact_contract import parse_submission
from strict_artifact_io_hardened import ArtifactError, parse_strict_json
from validation_ledger import RequestRejected
from scoring import final_result, trusted_history


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,sort_keys=True,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(temporary,path)


def invalid_result(reason):
    name = 'macro_verified_scaffold_hop_rate_at_10'
    return {'schema_version':'1.0','task_id':'synthesizable-scaffold-hopping',
        'valid':False,'status':'invalid','error':reason,
        'primary_metric':{'name':name,'value':0.,'direction':'maximize'},
        'metrics':[{'name':name,'value':0.,'unit':None}]}


def run_test(input_root, submission, result_path, context):
    runtime = load_runtime(input_root)
    # Actual trusted feedback history, not submitted evidence or budget_use.
    history = trusted_history(context,runtime)
    context.log({'phase':'test','trusted_feedback_history':history})
    try:
        records, parsing = parse_submission(Path(submission),instances=runtime.instances,byte_caps=BYTE_CAPS)
        result, _ = final_result(runtime.instances,records,context=context)
        context.log({'phase':'test','artifact_parsing':parsing})
    except (ArtifactError,RequestRejected) as exc:
        result = invalid_result(str(exc))
    atomic_json(result_path,result)
    return result


def run_validation(input_root, request_path, response_path, context):
    with Path(request_path).open('rb') as stream:
        raw = stream.read(REQUEST_BYTES+1)
    try:
        if len(raw)>REQUEST_BYTES:
            raise ArtifactError('request exceeds 1048576 bytes')
        request = parse_strict_json(raw)
    except ArtifactError as exc:
        response = {'status':'rejected','reason':str(exc),'charged':False}
    else:
        response = load_runtime(input_root).run(request,context)
    atomic_json(response_path,response)
    return response


def main(action):
    from science_innovation_exam.verify_context import VerifyContext
    parser = argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    if action=='test':
        parser.add_argument('--submission',type=Path,required=True)
        parser.add_argument('--result',type=Path,required=True)
    elif action=='validation':
        parser.add_argument('--request',type=Path,required=True)
        parser.add_argument('--response',type=Path,required=True)
    else:
        raise ValueError('unknown framework action')
    args = parser.parse_args()
    context = VerifyContext.current()
    if action=='test':
        run_test(args.input,args.submission,args.result,context)
    else:
        run_validation(args.input,args.request,args.response,context)
    return 0
