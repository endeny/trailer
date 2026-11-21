#!/usr/bin/env python3
"""Snapshot the TMDB popular movie list through iPlay's existing metadata API."""
import argparse
import datetime as dt
import json
from pathlib import Path
import urllib.parse
import urllib.request


def popular_page(page):
    path = '/3/movie/popular'
    from tmdb_proxy import headers as proxy_headers
    headers = proxy_headers(path)
    url = 'https://tmdb.saltpi.cn'+path+'?'+urllib.parse.urlencode({'language':'en-US','page':page})
    with urllib.request.urlopen(urllib.request.Request(url,headers=headers),timeout=45) as response:
        return json.load(response)


def snapshot(count=200, fetch=popular_page):
    if not 1 <= count <= 200: raise ValueError('invalid_count')
    items, seen = [], set()
    for page in range(1, 21):
        data = fetch(page)
        results = data.get('results')
        if not isinstance(results,list) or not results: raise ValueError('incomplete_popular_list')
        for movie in results:
            media_id = movie.get('id')
            if type(media_id) is not int or not 0 < media_id < 10**10 or media_id in seen: continue
            seen.add(media_id)
            items.append({'media_type':'movie','tmdb_id':media_id,'title':movie.get('title','')})
            if len(items) == count:
                return {'schema_version':1, 'source':'https://developer.themoviedb.org/reference/movie-popular-list',
                        'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(), 'items':items}
    raise ValueError('incomplete_popular_list')


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    try:
        plan=snapshot();path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
        print(f"Prepared {len(plan['items'])} unique popular movie identities.")
    except Exception:
        print('Popular snapshot failed; no partial list published.');raise SystemExit(1) from None
