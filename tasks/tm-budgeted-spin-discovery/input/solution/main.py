"""JSON-lines protocol starter. Replace selection with your frozen strategy."""
import json
import sys
candidate_ids = []
seen = set()
for line in sys.stdin:
    request = json.loads(line)
    if request['action'] == 'initialize':
        candidate_ids = [row['candidate_id'] for row in request['candidates']]
        seen = {row['candidate_id'] for row in request['initial_labels']}
        response = {'status': 'ready'}
    elif request['action'] == 'choose':
        feedback = request['feedback']
        if feedback is not None:
            seen.add(feedback['candidate_id'])
        response = {'candidate_id': next((i for i in candidate_ids if i not in seen), None)}
    else:
        raise ValueError('unknown action')
    print(json.dumps(response, allow_nan=False), flush=True)
