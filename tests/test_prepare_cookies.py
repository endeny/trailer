import importlib.util
from pathlib import Path
import json
import unittest

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'scripts/prepare_cookies.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class CookiePreparation(unittest.TestCase):
    def test_filters_large_browser_export_and_preserves_httponly(self):
        unrelated = 'example.com\tFALSE\t/\tTRUE\t0\tSESSION\t' + 'x' * 60000
        youtube = '#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsynthetic-value'
        filtered, count = prepare.filter_cookies('# Netscape HTTP Cookie File\n' + unrelated + '\n' + youtube + '\n')
        self.assertEqual(count, 1)
        self.assertNotIn(b'example.com', filtered)
        self.assertIn(b'#HttpOnly_.youtube.com', filtered)
        self.assertLess(len(filtered), 48 * 1024)

    def test_rejects_deceptive_domains_and_empty_selection(self):
        for domain in ['youtube.com.evil.example', 'notyoutube.com', 'google.com']:
            with self.assertRaises(prepare.CookieError):
                prepare.filter_cookies(f'{domain}\tFALSE\t/\tTRUE\t0\tSID\tfake\n')

    def test_converts_browser_json_to_netscape(self):
        value = [{'domain': '.youtube.com', 'path': '/', 'name': 'SID', 'value': 'fake', 'httpOnly': True, 'secure': True, 'expirationDate': 1800000000},
                 {'domain': 'other.example', 'name': 'SID', 'value': 'unrelated'}]
        filtered, count = prepare.filter_cookies(json.dumps(value))
        self.assertEqual(count, 1)
        self.assertIn(b'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1800000000\tSID\tfake', filtered)
        self.assertNotIn(b'unrelated', filtered)

    def test_rejects_oversized_youtube_only_export(self):
        with self.assertRaisesRegex(prepare.CookieError, 'exceed_secret_limit'):
            prepare.filter_cookies('.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t' + 'x' * 50000)

    def test_last_cookie_wins_for_same_scope(self):
        value = '.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tfirst\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecond\n'
        filtered, count = prepare.filter_cookies(value)
        self.assertEqual(count, 1)
        self.assertNotIn(b'first', filtered)
        self.assertIn(b'second', filtered)


if __name__ == '__main__': unittest.main()
