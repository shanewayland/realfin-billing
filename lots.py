"""
Bulk lot creation.

POST /generate-lots    (header X-Lots-Key must equal env LOTS_KEY)

WHY THIS EXISTS
  The Lot Release page used to create lots with a recursive backend workflow:
  one Bubble server run per lot, each scheduling the next. 240 lots cost ~170
  workload units, and when one run in the chain failed the chain died silently
  - Holly asked for 240 lots and got about 160, with no error shown.

  This endpoint creates every lot in ONE Bubble Data API bulk call, so there is
  no chain to break, and it reports exactly how many rows now exist.

Body: {"loan": "<bubble id>", "section": "Ph 1", "block": "Blk 1",
       "lots": "1-22, 25, 30-34", "version": "live"|"test"}

Returns: {"requested": n, "created": n, "skipped_existing": n, "failed": [...],
          "total_now": n}

Env: BUBBLE_API_TOKEN, LOTS_KEY, BUBBLE_BASE (optional).
The Data API must be enabled for the lot release type in Bubble
(Settings -> API -> Enable Data API, tick the lot release type).
"""

import hmac
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, jsonify, request

bp = Blueprint('lots', __name__)

MAX_LOTS = 600          # one click cannot ask for more than this
BULK_CHUNK = 250        # rows per bulk request


# ------------------------------------------------------------------ parsing

def parse_lots(text):
    """'1-22, 25, 30-34' -> [1..22, 25, 30..34]. Raises ValueError on junk."""
    if text is None:
        raise ValueError('no lots given')
    nums, seen = [], set()
    for part in str(text).replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r'(\d+)\s*[-–]\s*(\d+)', part)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            if end < start:
                raise ValueError(f'range runs backwards: {part}')
            if end - start + 1 > MAX_LOTS:
                raise ValueError(f'range too large: {part}')
            span = range(start, end + 1)
        elif re.fullmatch(r'\d+', part):
            span = [int(part)]
        else:
            raise ValueError(f'cannot read "{part}" as a lot or a range')
        for n in span:
            if n not in seen:
                seen.add(n)
                nums.append(n)
    if not nums:
        raise ValueError('no lots given')
    if len(nums) > MAX_LOTS:
        raise ValueError(f'{len(nums)} lots asked for, limit is {MAX_LOTS}')
    return nums


# ------------------------------------------------------------------ bubble

def _base(version):
    base = os.environ.get('BUBBLE_BASE', 'https://realfin.elevateebs.com').rstrip('/')
    return f"{base}/version-test" if version == 'test' else base


def _req(method, url, data=None, content_type='application/json'):
    headers = {'Authorization': f"Bearer {os.environ['BUBBLE_API_TOKEN']}"}
    if data is not None:
        headers['Content-Type'] = content_type
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode()


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', str(s).lower())


def discover(version):
    """Find the lot type's Data API slug and its field keys from swagger.

    Nothing is hard-coded: if the type or a field is renamed in Bubble this
    reports a clear error instead of silently writing to the wrong field.
    """
    body = _req('GET', f'{_base(version)}/api/1.1/meta/swagger.json')
    spec = json.loads(body)
    slug = None
    for path in spec.get('paths', {}):
        m = re.fullmatch(r'/obj/([a-z0-9_]+)', path)
        if m and 'lot' in m.group(1) and 'release' in m.group(1):
            slug = m.group(1)
            break
    if not slug:
        raise LookupError(
            'the lot release type is not exposed on the Data API - in Bubble, '
            'Settings > API > Enable Data API and tick the lot release type')
    props = ((spec.get('definitions') or {}).get(slug) or {}).get('properties') or {}
    idx = {_norm(k): k for k in props}
    fields = {}
    for want, keys in (('loan', ('loan',)),
                       ('section', ('tier1', 'tier1_', 'section')),
                       ('block', ('tier2', 'tier2_', 'block')),
                       ('lot', ('tier3', 'tier3_', 'lotnumber', 'lot'))):
        for k in keys:
            if _norm(k) in idx:
                fields[want] = idx[_norm(k)]
                break
        if want not in fields:
            raise LookupError(f'no {want} field on the {slug} type ({sorted(props)})')
    return slug, fields


def existing_lots(version, slug, fields, loan_id):
    """Lot numbers already on this loan, so a second click cannot duplicate."""
    found, cursor = set(), 0
    constraints = json.dumps([{'key': fields['loan'],
                               'constraint_type': 'equals', 'value': loan_id}])
    while True:
        url = (f'{_base(version)}/api/1.1/obj/{slug}'
               f'?limit=100&cursor={cursor}'
               f'&constraints={urllib.parse.quote(constraints)}')
        body = json.loads(_req('GET', url)).get('response', {})
        for row in body.get('results', []):
            n = row.get(fields['lot'])
            if n not in (None, ''):
                found.add(int(float(n)))
        cursor += len(body.get('results', []))
        if body.get('remaining', 0) <= 0 or not body.get('results'):
            return found


def bulk_create(version, slug, rows):
    """One Data API bulk call per chunk. Returns (created, failures)."""
    created, failures = 0, []
    for i in range(0, len(rows), BULK_CHUNK):
        chunk = rows[i:i + BULK_CHUNK]
        payload = '\n'.join(json.dumps(r) for r in chunk).encode()
        body = _req('POST', f'{_base(version)}/api/1.1/obj/{slug}/bulk',
                    data=payload, content_type='text/plain')
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                res = json.loads(line)
            except ValueError:
                failures.append(line[:200])
                continue
            if res.get('status') == 'success':
                created += 1
            else:
                failures.append(str(res)[:200])
    return created, failures


# ------------------------------------------------------------------ endpoint

@bp.route('/generate-lots', methods=['POST'])
def generate_lots():
    key = os.environ.get('LOTS_KEY')
    if key and not hmac.compare_digest(request.headers.get('X-Lots-Key', ''), key):
        return jsonify({'error': 'bad key'}), 403

    data = request.get_json(silent=True) or {}
    loan_id = (data.get('loan') or '').strip()
    section = (data.get('section') or '').strip()
    block = (data.get('block') or '').strip()
    version = 'test' if data.get('version') == 'test' else 'live'
    if not loan_id or not section or not block:
        return jsonify({'error': 'loan, section and block are all required'}), 400

    try:
        wanted = parse_lots(data.get('lots'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    try:
        slug, fields = discover(version)
        already = existing_lots(version, slug, fields, loan_id)
        todo = [n for n in wanted if n not in already]
        rows = [{fields['loan']: loan_id, fields['section']: section,
                 fields['block']: block, fields['lot']: n} for n in todo]
        created, failures = bulk_create(version, slug, rows) if rows else (0, [])
        total_now = len(existing_lots(version, slug, fields, loan_id))
    except LookupError as e:
        return jsonify({'error': str(e)}), 400
    except urllib.error.HTTPError as e:
        return jsonify({'error': f'bubble said {e.code}: {e.read().decode()[:300]}'}), 502

    return jsonify({'requested': len(wanted),
                    'created': created,
                    'skipped_existing': len(wanted) - len(todo),
                    'failed': failures,
                    'total_now': total_now}), (200 if not failures else 207)
