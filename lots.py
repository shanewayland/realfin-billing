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


SLUG_CANDIDATES = ('lot_release', 'lotrelease', 'lot_releases', 'lotreleases')

FIELD_GUESSES = {'loan': ('loan',),
                 'section': ('tier1', 'tier1_', 'section'),
                 'block': ('tier2', 'tier2_', 'block'),
                 'lot': ('tier3', 'tier3_', 'lotnumber', 'lot')}


def _match_fields(keys, slug):
    idx = {_norm(k): k for k in keys}
    fields = {}
    for want, guesses in FIELD_GUESSES.items():
        for g in guesses:
            if _norm(g) in idx:
                fields[want] = idx[_norm(g)]
                break
        if want not in fields:
            raise LookupError(f'no {want} field on the {slug} type ({sorted(keys)})')
    return fields


def discover(version):
    """Find the lot type's Data API slug and its field keys.

    Reads the swagger when it is published. Bubble can hide that (Settings >
    API > "Hide Swagger API documentation access"), so the fallback asks the
    Data API for one existing lot row and takes the field names off it.
    Nothing is hard-coded blindly: if neither works this raises rather than
    writing to a guessed field.
    """
    try:
        spec = json.loads(_req('GET', f'{_base(version)}/api/1.1/meta/swagger.json'))
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        spec = None

    if spec:
        for path in spec.get('paths', {}):
            m = re.fullmatch(r'/obj/([a-z0-9_]+)', path)
            if m and 'lot' in m.group(1) and 'release' in m.group(1):
                slug = m.group(1)
                props = ((spec.get('definitions') or {}).get(slug) or {}).get('properties') or {}
                if props:
                    return slug, _match_fields(props.keys(), slug)

    for slug in SLUG_CANDIDATES:
        try:
            body = json.loads(_req('GET', f'{_base(version)}/api/1.1/obj/{slug}?limit=1'))
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            continue
        results = (body.get('response') or {}).get('results') or []
        if not results:
            raise LookupError(
                f'the {slug} type has no rows yet, so the field names cannot be '
                'read - create one lot by hand and try again')
        keys = [k for k in results[0] if not k.startswith('_')]
        return slug, _match_fields(keys, slug)

    raise LookupError(
        'the lot release type is not reachable on the Data API - in Bubble, '
        'Settings > API > Enable Data API and tick the lot release type')


def existing_lots(version, slug, fields, loan_id):
    """What is already on this loan: (section, block, lot) triples, plus a count.

    Keyed on all three, not the lot number alone: Ph 2 / Blk 2 / lot 1 is a
    different lot from Ph 1 / Blk 1 / lot 1, and skipping it would silently
    leave a hole at the start of every new block.
    """
    found, total, cursor = set(), 0, 0
    constraints = json.dumps([{'key': fields['loan'],
                               'constraint_type': 'equals', 'value': loan_id}])
    while True:
        url = (f'{_base(version)}/api/1.1/obj/{slug}'
               f'?limit=100&cursor={cursor}'
               f'&constraints={urllib.parse.quote(constraints)}')
        body = json.loads(_req('GET', url)).get('response', {})
        for row in body.get('results', []):
            total += 1
            n = row.get(fields['lot'])
            if n not in (None, ''):
                found.add((_key(row.get(fields['section'])),
                           _key(row.get(fields['block'])),
                           int(float(n))))
        cursor += len(body.get('results', []))
        if body.get('remaining', 0) <= 0 or not body.get('results'):
            return found, total


def _key(v):
    """Section/block compared case- and space-insensitively: 'Blk 1' == 'blk 1'."""
    return re.sub(r'\s+', ' ', str(v or '').strip()).lower()


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
        already, _ = existing_lots(version, slug, fields, loan_id)
        here = (_key(section), _key(block))
        todo = [n for n in wanted if (here[0], here[1], n) not in already]
        rows = [{fields['loan']: loan_id, fields['section']: section,
                 fields['block']: block, fields['lot']: n} for n in todo]
        created, failures = bulk_create(version, slug, rows) if rows else (0, [])
        _, total_now = existing_lots(version, slug, fields, loan_id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 400
    except urllib.error.HTTPError as e:
        return jsonify({'error': f'bubble said {e.code}: {e.read().decode()[:300]}'}), 502

    return jsonify({'requested': len(wanted),
                    'created': created,
                    'skipped_existing': len(wanted) - len(todo),
                    'failed': failures,
                    'total_now': total_now}), (200 if not failures else 207)
