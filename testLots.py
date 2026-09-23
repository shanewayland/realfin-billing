"""Tests for bulk lot creation. No network: Bubble's HTTP layer is faked."""
import json
import os
import unittest
import urllib.error

os.environ.setdefault('BUBBLE_API_TOKEN', 'test-token')
os.environ.pop('LOTS_KEY', None)

import lots


SWAGGER = {
    'paths': {'/obj/lot_release': {}, '/obj/loan': {}},
    'definitions': {'lot_release': {'properties': {
        'loan': {}, 'tier1_': {}, 'tier2': {}, 'tier3': {}, 'note': {}}}},
}


class FakeBubble:
    """Stands in for _req: swagger, a list endpoint and a bulk endpoint."""

    def __init__(self, existing=(), fail_every=None):
        # existing: lot numbers (in Ph 1 / Blk 1) or full (section, block, lot)
        self.rows = []
        for e in existing:
            sec, blk, n = e if isinstance(e, tuple) else ('Ph 1', 'Blk 1', e)
            self.rows.append({'tier1_': sec, 'tier2': blk, 'tier3': n})
        self.fail_every = fail_every
        self.bulk_calls = 0

    def __call__(self, method, url, data=None, content_type='application/json'):
        if 'swagger' in url:
            return json.dumps(SWAGGER)
        if url.endswith('/bulk'):
            self.bulk_calls += 1
            out = []
            for i, line in enumerate(data.decode().splitlines()):
                row = json.loads(line)
                if self.fail_every and (i + 1) % self.fail_every == 0:
                    out.append(json.dumps({'status': 'error', 'message': 'nope'}))
                else:
                    self.rows.append(row)
                    out.append(json.dumps({'status': 'success', 'id': str(i)}))
            return '\n'.join(out)
        # list endpoint, one page
        return json.dumps({'response': {'results': self.rows,
                                        'remaining': 0, 'count': len(self.rows)}})


class ParseLots(unittest.TestCase):

    def test_range(self):
        self.assertEqual(lots.parse_lots('1-6'), [1, 2, 3, 4, 5, 6])

    def test_mixed(self):
        self.assertEqual(lots.parse_lots('1-3, 7, 10-11'), [1, 2, 3, 7, 10, 11])

    def test_spaces_and_dupes(self):
        self.assertEqual(lots.parse_lots(' 2 , 2, 1-2 '), [2, 1])

    def test_240_lots(self):
        self.assertEqual(len(lots.parse_lots('1-240')), 240)

    def test_backwards_range(self):
        with self.assertRaises(ValueError):
            lots.parse_lots('10-4')

    def test_junk(self):
        with self.assertRaises(ValueError):
            lots.parse_lots('1-3, abc')

    def test_empty(self):
        with self.assertRaises(ValueError):
            lots.parse_lots('  ')

    def test_over_limit(self):
        with self.assertRaises(ValueError):
            lots.parse_lots(f'1-{lots.MAX_LOTS + 1}')


class Discover(unittest.TestCase):

    def setUp(self):
        self._real = lots._req
        lots._req = FakeBubble()

    def tearDown(self):
        lots._req = self._real

    def test_finds_slug_and_fields(self):
        slug, fields = lots.discover('test')
        self.assertEqual(slug, 'lot_release')
        self.assertEqual(fields, {'loan': 'loan', 'section': 'tier1_',
                                  'block': 'tier2', 'lot': 'tier3'})

    def test_data_api_off(self):
        def dead(*a, **k):
            raise urllib.error.HTTPError('u', 404, 'nope', None, None)
        lots._req = dead
        with self.assertRaises(LookupError):
            lots.discover('test')

    def test_swagger_hidden_falls_back_to_a_record(self):
        """Bubble can hide the swagger; field names come off a real row instead."""
        def no_swagger(method, url, data=None, content_type='application/json'):
            if 'swagger' in url:
                raise urllib.error.HTTPError(url, 404, 'hidden', None, None)
            return json.dumps({'response': {'results': [
                {'_id': 'x', 'loan': 'loan1', 'tier1_': 'Ph 1', 'tier2': 'Blk',
                 'tier3': 4, 'note': ''}], 'remaining': 0}})
        lots._req = no_swagger
        slug, fields = lots.discover('test')
        self.assertEqual(slug, 'lot_release')
        self.assertEqual(fields, {'loan': 'loan', 'section': 'tier1_',
                                  'block': 'tier2', 'lot': 'tier3'})

    def test_no_rows_yet_is_a_clear_error(self):
        def empty(method, url, data=None, content_type='application/json'):
            if 'swagger' in url:
                raise urllib.error.HTTPError(url, 404, 'hidden', None, None)
            return json.dumps({'response': {'results': [], 'remaining': 0}})
        lots._req = empty
        with self.assertRaisesRegex(LookupError, 'no rows yet'):
            lots.discover('test')


class Endpoint(unittest.TestCase):

    def setUp(self):
        self._real = lots._req
        from service import app
        app.config['TESTING'] = True
        self.client = app.test_client()

    def tearDown(self):
        lots._req = self._real

    def post(self, **body):
        body.setdefault('loan', 'loan1')
        body.setdefault('section', 'Ph 1')
        body.setdefault('block', 'Blk 1')
        body.setdefault('version', 'test')
        return self.client.post('/generate-lots', json=body)

    def test_240_in_one_call_each_chunk(self):
        fake = lots._req = FakeBubble()
        r = self.post(lots='1-240')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['created'], 240)
        self.assertEqual(r.get_json()['total_now'], 240)
        # 240 lots must not be 240 requests
        self.assertLessEqual(fake.bulk_calls, 2)

    def test_skips_lots_already_there(self):
        lots._req = FakeBubble(existing=[1, 2, 3])
        body = self.post(lots='1-5', section='Ph 1', block='Blk 1').get_json()
        self.assertEqual(body['created'], 2)
        self.assertEqual(body['skipped_existing'], 3)
        self.assertEqual(body['total_now'], 5)

    def test_partial_failure_is_reported(self):
        lots._req = FakeBubble(fail_every=5)
        r = self.post(lots='1-10')
        self.assertEqual(r.status_code, 207)
        body = r.get_json()
        self.assertEqual(body['created'], 8)
        self.assertEqual(len(body['failed']), 2)

    def test_same_lot_number_in_another_block_is_not_a_duplicate(self):
        """Ph 1/Blk 1 lots 1-3 must not block Ph 2/Blk 2 lots 1-3."""
        lots._req = FakeBubble(existing=[('Ph 1', 'Blk 1', 1), ('Ph 1', 'Blk 1', 2),
                                         ('Ph 1', 'Blk 1', 3)])
        body = self.post(lots='1-3', section='Ph 2', block='Blk 2').get_json()
        self.assertEqual(body['created'], 3)
        self.assertEqual(body['skipped_existing'], 0)
        self.assertEqual(body['total_now'], 6)

    def test_block_match_ignores_case_and_spacing(self):
        lots._req = FakeBubble(existing=[('Ph 1', 'Blk 1', 1)])
        body = self.post(lots='1-2', section=' ph 1 ', block='BLK  1').get_json()
        self.assertEqual(body['skipped_existing'], 1)
        self.assertEqual(body['created'], 1)

    def test_bad_lots_string(self):
        lots._req = FakeBubble()
        r = self.post(lots='1-3, oops')
        self.assertEqual(r.status_code, 400)

    def test_missing_section(self):
        lots._req = FakeBubble()
        r = self.post(lots='1-3', section='')
        self.assertEqual(r.status_code, 400)

    def test_key_required_when_set(self):
        lots._req = FakeBubble()
        os.environ['LOTS_KEY'] = 'secret'
        try:
            self.assertEqual(self.post(lots='1-3').status_code, 403)
            r = self.client.post('/generate-lots', headers={'X-Lots-Key': 'secret'},
                                 json={'loan': 'loan1', 'section': 'A', 'block': 'B',
                                       'lots': '1-3', 'version': 'test'})
            self.assertEqual(r.status_code, 200)
        finally:
            os.environ.pop('LOTS_KEY')


if __name__ == '__main__':
    unittest.main(verbosity=2)
