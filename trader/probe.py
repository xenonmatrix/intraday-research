"""Time-bounded read-only observation mode; never changes execution authorization."""
import json
from .models import dt


def active(runtime,now):
    if not (runtime/'MAINTENANCE').exists():return False
    try:
        marker=json.loads((runtime/'READ_ONLY_SOAK.json').read_text())
        return marker.get('read_only') is True and dt(marker['start'])<=now<dt(marker['expires'])
    except (OSError,ValueError,KeyError,TypeError):return False
