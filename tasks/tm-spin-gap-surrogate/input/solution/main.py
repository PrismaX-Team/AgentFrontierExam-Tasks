"""JSON-lines protocol starter. Replace zeros with frozen model inference."""
import json
import sys


for line in sys.stdin:
    request = json.loads(line)
    if request['action'] != 'predict':
        raise ValueError('unknown action')
    response = {
        'predictions': [
            {
                'sample_id': row['sample_id'],
                'predicted_gap_kcal_mol': 0.0,
            }
            for row in request['records']
        ]
    }
    print(json.dumps(response, allow_nan=False), flush=True)
