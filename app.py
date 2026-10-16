"""Live Knesset election results: polls the Central Elections Committee site politely,
computes the Bader-Ofer seat allocation, and serves a local UI. Start with start.bat."""
import html
import json
import re
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'config.json'
SNAPSHOTS = HERE / 'snapshots'
PORT = 8026

session = requests.Session()
# The site's firewall serves an error page to non-browser user agents.
session.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                 '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')

cfg = json.loads(CONFIG.read_text('utf-8'))
state = {'version': 0, 'status': 'Starting…', 'last_check': None, 'last_change': None, 'national': None}
page_cache = {}  # url -> (national version when fetched, parsed page or option list)
wake = threading.Event()


def site():
    return f"https://votes{cfg['knesset']}.bechirot.gov.il/"


def fetch(path):
    r = session.get(site() + path, timeout=30)
    r.raise_for_status()
    if 'maintenance.gov.il' in r.url:
        raise RuntimeError('site redirected to the gov.il error page (not live yet, or blocked)')
    r.encoding = 'utf-8'
    return r.text


def text(fragment):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', fragment))).replace('‏', '').strip()


def parse_results(page):
    """Summary rows and per-list votes from a national, city or ballot results page."""
    summary = re.search(r'class="ResultsSummaryTable"(.*?)</table>', page, re.S)
    body = re.search(r'class="TableData".*?<tbody>(.*?)</tbody>', page, re.S)
    if not (summary and body):
        raise ValueError('no results table on the page')
    heads = [text(c) for c in re.findall(r'<th[^>]*>(.*?)</th>', summary.group(1), re.S)]
    vals = [text(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', summary.group(1), re.S)]
    lists = []
    for row in re.findall(r'<tr.*?</tr>', body.group(1), re.S):
        cells = re.findall(r'<td([^>]*)>(.*?)</td>', row, re.S)
        # Names contain " (e.g. חד"ש), so match the title attribute up to its last quote, not the first.
        title = re.search(r'title=([\'"])(.*)\1', cells[0][0])
        lists.append({'letters': text(cells[1][1]),
                      'name': html.unescape(title.group(2)) if title else text(cells[0][1]),
                      'votes': int(text(cells[-1][1]).replace(',', ''))})
    status = re.search(r'<h3 class="LastUpdate">(.*?)</h3>', page, re.S)
    return {'summary': list(zip(heads, vals)), 'lists': lists, 'status': text(status.group(1)) if status else ''}


def parse_options(page, datalist):
    """(value, label) pairs of a <datalist> (select1 = municipalities, select2 = polling stations)."""
    block = page.split(f'id={datalist}', 1)[1].split('</datalist>', 1)[0]
    return [(v, text(n)) for v, n in re.findall(r'<option[^>]*value=["\']?([^"\' >]+)[^>]*>([^<]*)', block, re.I)
            if v != '0']


def dhondt(votes, seats):
    won = dict.fromkeys(votes, 0)
    for _ in range(seats if votes else 0):
        best = max(votes, key=lambda k: votes[k] / (won[k] + 1))
        won[best] += 1
    return won


def allocate(votes, seats=120, threshold=0.0325, agreements=()):
    """Knesset Elections Law s.81: lists under the threshold are dropped; the rest share the seats by
    Bader-Ofer (equivalent to D'Hondt), with each surplus-agreement pair counted as one list, and the
    pair's seats then split between its two lists by the same method. Pairs need both lists to pass."""
    total = sum(votes.values())
    passed = {k: v for k, v in votes.items() if v and v >= threshold * total}
    group = {k: (k,) for k in passed}
    for a, b in agreements:
        if a != b and group.get(a) == (a,) and group.get(b) == (b,):
            group[a] = group[b] = (a, b)
    blocs = {g: sum(passed[k] for k in g) for g in set(group.values())}
    result = dict.fromkeys(votes, 0)
    for g, n in dhondt(blocs, seats).items():
        result.update(dhondt({k: passed[k] for k in g}, n))
    return result


def cached(url, parse, live=True):
    """Fetch a page once; re-fetch a live results page only after the national results changed."""
    hit = page_cache.get(url)
    if not hit or (live and hit[0] != state['version']):
        hit = page_cache[url] = (state['version'], parse(fetch(url)))
    return hit[1]


def results(city=None, ballot=None):
    if city:
        url = f'ballotresults?cityID={city}&BallotNumber={ballot}' if ballot else f'cityresults?cityID={city}'
        data = cached(url, parse_results)
    elif state['national']:
        data = state['national']
    else:
        raise RuntimeError('no national results yet')
    seats = allocate({l['letters']: l['votes'] for l in data['lists']},
                     cfg['seats'], cfg['threshold'], cfg['elections'].get(str(cfg['knesset']), {}).get('agreements', []))
    return {**data, 'lists': [{**l, 'seats': seats[l['letters']]} for l in data['lists']]}


def monitor():
    while True:
        try:
            data = parse_results(fetch('nationalresults'))
            now = datetime.now().isoformat(timespec='seconds')
            state['last_check'] = now
            if data != state['national']:
                state.update(national=data, last_change=now, version=state['version'] + 1)
                SNAPSHOTS.mkdir(exist_ok=True)
                (SNAPSHOTS / f"k{cfg['knesset']}_national_{now.replace(':', '')}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
                print(f'{now}  new results')
            state['status'] = 'ok'
        except Exception as e:  # keep the last good results and retry next cycle
            state['status'] = f'{type(e).__name__}: {e}'
            print(datetime.now().strftime('%H:%M:%S'), state['status'])
        wake.wait(max(30, cfg['poll_seconds']))  # the site itself caches pages for 60 s
        wake.clear()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/':
                return self.send(200, (HERE / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if url.path == '/api/state':
                return self.send(200, {k: state[k] for k in ('version', 'status', 'last_check', 'last_change')}
                                 | {'config': cfg, 'site': site()})
            if url.path == '/api/cities':
                return self.send(200, cached('cityresults', lambda p: parse_options(p, 'select1'), False))
            if url.path == '/api/ballots':
                return self.send(200, cached(f"ballotresults?cityID={q['city']}", lambda p: parse_options(p, 'select2'), False))
            if url.path == '/api/results':
                return self.send(200, results(q.get('city'), q.get('ballot')))
            self.send(404, {'error': 'not found'})
        except Exception as e:
            self.send(502, {'error': f'{type(e).__name__}: {e}'})

    def do_POST(self):  # /api/config: the UI saves the whole config object
        new = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        switched = new.get('knesset') != cfg['knesset']
        cfg.update(new)
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
        if switched:
            page_cache.clear()
            state.update(national=None, version=state['version'] + 1, status='Switching election…')
            wake.set()
        self.send(200, {'ok': True})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError:  # already running: just open the UI
        webbrowser.open(f'http://localhost:{PORT}')
        raise SystemExit
    threading.Thread(target=monitor, daemon=True).start()
    print(f'Knesset results UI on http://localhost:{PORT} - close this window to stop.')
    webbrowser.open(f'http://localhost:{PORT}')
    server.serve_forever()
