#!/usr/bin/env python3
"""Keep only YouTube cookies and upload through gh stdin without printing secrets."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

MAX_SECRET_BYTES = 48 * 1024


class CookieError(Exception):
    pass


def youtube_domain(domain):
    domain = domain.lower().lstrip('.')
    return domain == 'youtube.com' or domain.endswith('.youtube.com')


def filter_cookies(text):
    text = text.lstrip('\ufeff')
    rows = []
    if text.lstrip().startswith(('[', '{')):
        try:
            data = json.loads(text)
        except ValueError:
            raise CookieError('invalid_cookie_json') from None
        if isinstance(data, dict):
            data = data.get('cookies')
        if not isinstance(data, list):
            raise CookieError('unsupported_cookie_json')
        for cookie in data:
            if not isinstance(cookie, dict) or not youtube_domain(str(cookie.get('domain', ''))):
                continue
            domain = str(cookie['domain'])
            if cookie.get('httpOnly'):
                domain = '#HttpOnly_' + domain
            expiry = cookie.get('expirationDate', cookie.get('expires', 0))
            try:
                expiry = str(max(0, int(float(expiry or 0))))
            except (ValueError, TypeError, OverflowError):
                raise CookieError('invalid_cookie_expiry') from None
            rows.append([domain, 'TRUE' if str(cookie['domain']).startswith('.') else 'FALSE',
                         str(cookie.get('path') or '/'), 'TRUE' if cookie.get('secure') else 'FALSE',
                         expiry, str(cookie.get('name', '')), str(cookie.get('value', ''))])
    else:
        for line in text.splitlines():
            if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
                continue
            fields = line.split('\t')
            domain = fields[0].removeprefix('#HttpOnly_')
            if not youtube_domain(domain):
                continue
            if len(fields) != 7:
                raise CookieError('invalid_netscape_cookie_row')
            rows.append(fields)
    cookies = {}
    for row in rows:
        if any('\n' in v or '\r' in v or '\t' in v for v in row) or not row[5]:
            raise CookieError('invalid_cookie_field')
        if row[1] not in ('TRUE', 'FALSE') or row[3] not in ('TRUE', 'FALSE') or not re.fullmatch(r'\d+', row[4]) or not row[2].startswith('/'):
            raise CookieError('invalid_netscape_cookie_row')
        cookies[(row[0].removeprefix('#HttpOnly_'), row[2], row[5])] = row
    if not cookies:
        raise CookieError('no_youtube_cookies')
    result = ('# Netscape HTTP Cookie File\n' + '\n'.join('\t'.join(row) for row in cookies.values()) + '\n').encode()
    if len(result) >= MAX_SECRET_BYTES:
        raise CookieError('youtube_cookies_still_exceed_secret_limit')
    return result, len(cookies)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('file', nargs='?')
    parser.add_argument('--from-env', action='store_true', help='Read YOUTUBE_COOKIES from the environment')
    parser.add_argument('--output', type=Path, help='Create a new mode-0600 Netscape file; never overwrite')
    parser.add_argument('--upload', action='store_true', help='Set the GitHub YOUTUBE_COOKIES secret via stdin')
    parser.add_argument('--repo', default='endeny/trailer')
    args = parser.parse_args()
    if bool(args.file) == bool(args.from_env):
        raise CookieError('choose_file_or_from_env')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        raise CookieError('invalid_repository')
    text = os.environ.get('YOUTUBE_COOKIES', '') if args.from_env else Path(args.file).read_text(encoding='utf-8-sig')
    filtered, count = filter_cookies(text)
    if args.output:
        with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
            output.write(filtered)
    if args.upload:
        result = subprocess.run(['gh', 'secret', 'set', 'YOUTUBE_COOKIES', '--repo', args.repo], input=filtered, capture_output=True)
        if result.returncode:
            raise CookieError('github_secret_upload_failed')
    print(f'YouTube cookies: {count}; filtered bytes: {len(filtered)}; uploaded: {args.upload}')


if __name__ == '__main__':
    try:
        main()
    except CookieError as error:
        print(f'Cookie preparation failed: {error}')
        raise SystemExit(1) from None
    except Exception:
        print('Cookie preparation failed: unable_to_read_or_write_file')
        raise SystemExit(1) from None
