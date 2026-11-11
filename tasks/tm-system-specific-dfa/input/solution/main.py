"""JSON-lines starter: select the first registered method for every record."""

import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    recommendations = [
        {'sample_id': row['sample_id'], 'method_id': request['method_ids'][0]}
        for row in request['records']
    ]
    print(json.dumps({'recommendations': recommendations}, allow_nan=False), flush=True)
