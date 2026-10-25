"""Live Knesset election results: polls the Central Elections Committee site politely,
computes the Bader-Ofer seat allocation, and serves the web UI. Start with start.bat, or see README for hosting."""
import csv
import gzip
import hashlib
import hmac
import html
import io
import json
import os
import re
import ssl
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'config.json'
SNAPSHOTS = HERE / 'snapshots'
HOST = os.environ.get('HOST', '127.0.0.1')  # 0.0.0.0 to serve other machines
PORT = int(os.environ.get('PORT', '8026'))
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')  # whoever opens /?admin=<token> can edit the shared settings
LOCAL = HOST in ('127.0.0.1', 'localhost')
# The Central Bureau of Statistics' spelling of each town (build_towns.py): the committee's files drop hyphens, quote
# marks and brackets, and run words together ("כעביהטבאשחגאגרה", "קודייראת אצאנעשבט").
BUREAU_NAMES = {code: t[7] for code, t in json.loads((HERE / 'data' / 'towns.json').read_text('utf-8')).items() if len(t) > 7}

class OldCipher(requests.adapters.HTTPAdapter):
    """The April 2019 results file server only offers AES128-SHA, which Python refuses by default. Allowing that one
    cipher keeps the connection encrypted and the certificate checked; the files are public anyway."""
    def init_poolmanager(self, *args, **kwargs):
        context = ssl.create_default_context()
        context.set_ciphers('DEFAULT:AES128-SHA')
        return super().init_poolmanager(*args, **kwargs, ssl_context=context)


session = requests.Session()
# The site's firewall serves an error page to non-browser user agents.
session.headers['User-Agent'] = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                 '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
session.mount('https://media21.bechirot.gov.il/', OldCipher())

cfg = json.loads(CONFIG.read_text('utf-8'))
state = {'version': 0, 'status': 'Starting…', 'last_check': None, 'last_change': None, 'national': None}
page_cache = {}  # url -> (national version when fetched, parsed page or option list)
wake = threading.Event()
fetch_lock = threading.Lock()
last_fetch = [0.0]


# Past elections whose final results the Central Elections Committee still hosts, in the same page format.
PAST = {25: 'November 2022', 24: 'March 2021', 23: 'March 2020', 22: 'September 2019', 21: 'April 2019'}


def site(kn=None):
    return f"https://votes{kn or cfg['knesset']}.bechirot.gov.il/"


def election(value):
    """The Knesset number a visitor asked for: the one followed live (the default) or a past one."""
    kn = int(value or cfg['knesset'])
    if kn != cfg['knesset'] and kn not in PAST:
        raise ValueError('unknown election')
    return kn


def get(url, **kw):
    # One request at a time, spaced out, however many visitors click around: keeps us off the site's blocklist.
    with fetch_lock:
        time.sleep(max(0.0, last_fetch[0] + 0.5 - time.monotonic()))
        try:
            return session.get(url, timeout=60, **kw)
        finally:
            last_fetch[0] = time.monotonic()


def fetch(path, kn=None):
    r = get(site(kn) + path)
    r.raise_for_status()
    if 'maintenance.gov.il' in r.url:
        raise RuntimeError('site redirected to the gov.il error page (not live yet, or blocked)')
    r.encoding = 'utf-8'
    return r.text


def text(fragment):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', fragment))).replace('\u200f', '').strip()


def parse_results(page):
    """Summary rows and per-list votes from a national, city or ballot results page."""
    summary = re.search(r'class="ResultsSummaryTable"(.*?)</table>', page, re.S)
    body = re.search(r'class="TableData".*?<tbody>(.*?)</tbody>', page, re.S)
    if not (summary and body):
        title = re.search(r'<title>([^<]*)', page)
        raise ValueError(f'no results table on the page ({len(page)} chars, title "{text(title.group(1)) if title else ""}")')
    heads = [text(c) for c in re.findall(r'<th[^>]*>(.*?)</th>', summary.group(1), re.S)]
    vals = [text(c) for c in re.findall(r'<td[^>]*>(.*?)</td>', summary.group(1), re.S)]
    # Final national results add an official seats column (מנדטים); live and local pages don't have one.
    columns = [text(c) for c in re.findall(r'<th[^>]*>(.*?)</th>', page[body.start():body.start(1)], re.S)]
    seat_col = next((i for i, c in enumerate(columns) if c.startswith('מנדטים')), None)
    lists = []
    for row in re.findall(r'<tr.*?</tr>', body.group(1), re.S):
        cells = re.findall(r'<td([^>]*)>(.*?)</td>', row, re.S)
        # Names contain " (e.g. חד"ש), so match the title attribute up to its last quote, not the first.
        title = re.search(r'title=([\'"])(.*)\1', cells[0][0])
        seats = text(cells[seat_col][1]) if seat_col is not None and seat_col < len(cells) else ''
        lists.append({'letters': text(cells[1][1]),
                      'name': html.unescape(title.group(2)) if title else text(cells[0][1]),
                      'votes': int(text(cells[-1][1]).replace(',', ''))}
                     | ({'seats': int(seats)} if seats.isdigit() else {}))
    # The site occasionally serves an odd variant of a page: trust official seats only if they fill the whole Knesset.
    if sum(l.get('seats', 0) for l in lists) != cfg['seats'] or any('seats' not in l for l in lists):
        for l in lists:
            l.pop('seats', None)
    status = re.search(r'<h3 class="LastUpdate">(.*?)</h3>', page, re.S)
    return {'summary': [list(p) for p in zip(heads, vals)], 'lists': lists, 'status': text(status.group(1)) if status else ''}


def town_name(code, name):
    """The bureau's spelling of a town's name, when it has the same letters as the committee's (so it is the same name)."""
    better, letters = BUREAU_NAMES.get(code), lambda s: re.sub('[^א-ת]', '', s)
    return better if better and letters(better) == letters(name) else name


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


def cached(kn, path, parse, live=False, on_change=None):
    """Fetch a page once. Results pages of the election followed live are re-fetched after its national results
    change; on_change is called with the page whenever that brings something new."""
    hit = page_cache.get((kn, path))
    if not hit or (live and kn == cfg['knesset'] and hit[0] != state['version']):
        try:
            parsed = parse(fetch(path, kn))
        except ValueError:  # the site now and then serves a one-off error page: try once more, after a pause
            time.sleep(2)
            parsed = parse(fetch(path, kn))
        if on_change and (not hit or hit[1] != parsed):
            on_change(parsed)
        hit = page_cache[(kn, path)] = (state['version'], parsed)
    return hit[1]


# Saved results: snapshots/k<knesset>_<kind>_<time>.<ext>, where kind is 'national', 'ballots' (the committee's
# per-polling-station spreadsheet) or 'place_<town>_<station or all>' (a page someone opened).
STAMP = r'\d{4}-\d\d-\d\dT\d{6}'


def saved(kn, kind):
    """Times of the files saved for one election and kind, oldest first."""
    return sorted(m.group(1) for f in SNAPSHOTS.glob(f'k{kn}_{kind}_*')
                  if (m := re.fullmatch(rf'k{kn}_{re.escape(kind)}_({STAMP})\..+', f.name)))


def save(kn, kind, data, t=None):
    SNAPSHOTS.mkdir(exist_ok=True)
    (SNAPSHOTS / f'k{kn}_{kind}_{t or datetime.now().astimezone():%Y-%m-%dT%H%M%S}.json').write_text(
        json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')


def parse_ballots(raw):
    """The committee's per-polling-station spreadsheet: list letters, and counts per polling station and per town.
    Each count is [eligible, voted, invalid, valid, then the votes of each list]."""
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:  # older spreadsheets use the Windows Hebrew encoding
        content = raw.decode('cp1255')
    rows = csv.reader(io.StringIO(content))
    head = next(rows)
    col = {h.strip(): i for i, h in enumerate(head)}
    col.setdefault('קלפי', col.get('מספר קלפי'))  # April 2019 calls the polling station column "מספר קלפי"
    if col['קלפי'] is None or not {'סמל ישוב', 'בזב', 'מצביעים', 'פסולים', 'כשרים'} <= col.keys():
        raise ValueError('the results spreadsheet has unexpected columns')
    # The lists' columns come after the valid votes; some years end the header with a stray comma, so skip blank names.
    lists = [(i, h.strip()) for i, h in enumerate(head) if i > col['כשרים'] and h.strip()]
    stations, towns, names = {}, {}, {}
    for r in rows:
        if len(r) != len(head):
            continue
        n = [int(r[col[h]] or 0) for h in ('בזב', 'מצביעים', 'פסולים', 'כשרים')] + [int(r[i] or 0) for i, _ in lists]
        stations[(r[col['סמל ישוב']], r[col['קלפי']])] = n
        names[r[col['סמל ישוב']]] = town_name(r[col['סמל ישוב']], r[col['שם ישוב']].strip()) if 'שם ישוב' in col else r[col['סמל ישוב']]
        town = towns.setdefault(r[col['סמל ישוב']], [0] * len(n))
        for i, x in enumerate(n):
            town[i] += x
    return [h for _, h in lists], stations, towns, names


ballot_cache, ballot_lock = {}, threading.Lock()


def load_ballots(kn, stamp):
    with ballot_lock:
        if (kn, stamp) not in ballot_cache:
            f = SNAPSHOTS / f'k{kn}_ballots_{stamp}.csv.gz'
            if not f.exists():
                raise ValueError('no results were saved at that time')
            if len(ballot_cache) >= 4:  # a few parsed versions are plenty for people sliding back and forth
                ballot_cache.pop(next(iter(ballot_cache)))
            ballot_cache[(kn, stamp)] = parse_ballots(gzip.decompress(f.read_bytes()))
        return ballot_cache[(kn, stamp)]


def from_ballots(parsed, city, ballot, national=None):
    """A town's or polling station's results from the spreadsheet, shaped like the site's own results page.
    List names come from the national results (the live election's by default)."""
    letters, stations, towns, _ = parsed
    n = stations.get((city, ballot)) if ballot else towns.get(city)
    if n is None:
        raise ValueError('the results spreadsheet has nothing for this place yet')
    names = {l['letters']: l['name'] for l in (national or state['national'] or {}).get('lists', [])}
    eligible, voted, invalid, valid = n[:4]
    return {'summary': [['סה"כ בעלי זכות בחירה', f'{eligible:,}'], ['סה"כ קולות', f'{voted:,}'],
                        ['שיעור ההצבעה', f'{voted / eligible:.2%}' if eligible else '-'],
                        ['סה"כ הקולות הכשרים', f'{valid:,}'], ['סה"כ הקולות הפסולים', f'{invalid:,}']],
            'status': 'לפי קובץ התוצאות לפי קלפיות',
            'lists': [{'letters': x, 'name': names.get(x, x), 'votes': v} for x, v in zip(letters, n[4:])]}


def final_ballots(kn):
    """A past election's per-polling-station spreadsheet. It's final, so it is downloaded once and kept."""
    f = SNAPSHOTS / f'k{kn}_ballots_final.csv.gz'
    for attempt in range(4):  # the file server now and then answers with an error page instead: be patient, it's once
        if f.exists():
            break
        try:
            r = get(f'https://media{kn}.bechirot.gov.il/files/expb.csv')
            r.raise_for_status()
            parse_ballots(r.content)  # refuse a file we can't read before saving it
            SNAPSHOTS.mkdir(exist_ok=True)
            f.write_bytes(gzip.compress(r.content))
        except (requests.RequestException, ValueError, UnicodeDecodeError):
            if attempt == 3:
                raise
            time.sleep(5)
    return load_ballots(kn, 'final')


final_cache = {}


def final_national(kn):
    """A past election's national results (with the official seats), fetched once and kept: they won't change."""
    if kn not in final_cache:
        f = SNAPSHOTS / f'k{kn}_national_final.json'
        if not f.exists():
            for attempt in range(4):  # the site now and then serves a variant without the tables: be patient, it's once
                try:
                    data = cached(kn, 'nationalresults', parse_results)
                    if all('seats' in l for l in data['lists']):
                        break
                except ValueError:
                    pass
                page_cache.pop((kn, 'nationalresults'), None)
                time.sleep(5)
            else:
                raise ValueError('the national results page came without official seats; try again in a minute')
            SNAPSHOTS.mkdir(exist_ok=True)
            f.write_text(json.dumps(data, ensure_ascii=False, indent=1), 'utf-8')
        final_cache[kn] = json.loads(f.read_text('utf-8'))
    return final_cache[kn]


def download_past():
    """Store every past election locally at startup, so visitors never wait for (or add load to) the elections site."""
    for kn in PAST:
        try:
            final_ballots(kn)
            final_national(kn)
        except Exception as e:  # tried again the next time someone opens that election
            print(f'Knesset {kn}: {type(e).__name__}: {e}', flush=True)


station_json = {}  # (knesset, version) -> the statistics page's data, ready to send


def sheet_version(kn):
    """Which spreadsheet the statistics and trends use: the live election's latest (when the admin has the check on
    and the committee publishes it), or a past election's final one."""
    if kn != cfg['knesset']:
        final_ballots(kn)
        return 'final'
    stamps = saved(kn, 'ballots') if cfg.get('poll_spreadsheet') else []
    if not stamps:
        raise LookupError('no per-polling-station spreadsheet for the live election yet')
    return stamps[-1]


town_json = {}  # (knesset, version) -> every town's totals, ready to send


def towns(kn):
    """Every town's totals, for the trends page: [name, eligible, voted, invalid, valid, then each list's votes]."""
    version = sheet_version(kn)
    if (kn, version) not in town_json:
        letters, _, totals, names = load_ballots(kn, version)
        for key in [key for key in town_json if key[0] == kn]:
            del town_json[key]
        town_json[(kn, version)] = json.dumps({'letters': letters, 'towns': {t: [names.get(t, t), *n] for t, n in totals.items()}},
                                              ensure_ascii=False, separators=(',', ':')).encode()
    return town_json[(kn, version)]


def stations(kn):
    """Every polling station's counts, for the statistics page."""
    version = sheet_version(kn)
    if (kn, version) not in station_json:
        letters, rows, _, towns = load_ballots(kn, version)
        try:
            names = {l['letters']: l['name'] for l in results(kn)['lists']}
        except Exception:  # national results not in yet: the page falls back to ballot letters
            names = {}
        for key in [key for key in station_json if key[0] == kn]:  # only the newest version is worth keeping
            del station_json[key]
        station_json[(kn, version)] = json.dumps({
            'letters': letters, 'names': names, 'towns': towns,
            'time': None if version == 'final' else datetime.strptime(version, '%Y-%m-%dT%H%M%S').astimezone().isoformat(),
            # each row: town, polling station, eligible, voted, invalid, valid, then the votes of each list
            'stations': [[town, ballot, *n] for (town, ballot), n in rows.items()],
        }, ensure_ascii=False, separators=(',', ':')).encode()
    return station_json[(kn, version)]


ENVELOPES = ('9999', '99999')  # double envelopes (soldiers, diplomats, patients, prisoners...): one "town", counted last


def project(live, prev, before, growth=1.0, prior=500):
    """Election night: where the count is heading. live and prev are parsed spreadsheets (this election's so far, the
    previous one's final); before maps each list to the previous lists its parties ran on; growth is the change in
    eligible voters since then. A town's uncounted voters are expected to turn out like its counted voters, or like last
    time adjusted by the change in turnout so far. They are expected to vote like its counted voters, moved by how
    differently its uncounted polling stations voted from its counted ones last time (Haredi neighbourhoods are often
    counted late), with every list's swing measured on the stations counted in both elections. Where nothing is counted
    yet, like last time, moved by each list's swing so far. New lists take the town's counted share, blended (as
    `prior` votes' worth) with their national one. Returns the projected votes per list, the votes counted so far per
    list, and each town's valid votes still to count, or None before anything is counted. Replayed on the 2019-2022
    elections with stations counted in a random, a town-by-town and a skewed order, it was as close as projecting from
    whole towns, and much closer when the order was skewed."""
    letters, rows, _, names = live
    p_letters, p_rows, p_towns, p_names = prev
    town = lambda t: ENVELOPES[0] if t in ENVELOPES else t
    lists_all = any(n[0] and not n[1] for n in rows.values())  # does the file list stations not counted yet?
    now = {}  # town -> [eligible listed, eligible counted, valid counted, votes counted per list]
    for (t, _), n in rows.items():
        a = now.setdefault(town(t), [0, 0, 0, [0] * len(letters)])
        a[0] += n[0]
        if n[1]:
            a[1] += n[0]
            a[2] += n[3]
            a[3] = [x + y for x, y in zip(a[3], n[4:])]
    old = {}
    for t, n in p_towns.items():
        o = old.setdefault(town(t), [0, 0, [0] * len(p_letters)])  # eligible, valid, votes per previous list
        o[0] += n[0]
        o[1] += n[3]
        o[2] = [x + y for x, y in zip(o[2], n[4:])]
    if not sum(a[2] for a in now.values()):
        return None
    cols = [[p_letters.index(x) for x in before.get(l, ()) if x in p_letters] for l in letters]
    was = lambda o: [sum(o[2][i] for i in c) / o[1] if o[1] else 0 for c in cols]  # predecessors' share last time
    then = lambda n: [sum(n[4 + i] for i in c) / n[3] if n[3] else 0 for c in cols]  # the same, for one polling station
    # Swing and turnout so far, measured where both elections have results.
    both = [(a, old[t]) for t, a in now.items() if a[2] and old.get(t, [0, 0])[1]]
    counted = sum(a[2] for a, _ in both) or 1
    share_now = [sum(a[3][j] for a, _ in both) / counted for j in range(len(letters))]
    share_was = [sum(a[2] * w for (a, _), w in zip(both, ws)) / counted for ws in zip(*[was(o) for _, o in both])] or [0] * len(letters)
    expected = sum(a[1] * o[1] / o[0] for a, o in both if o[0])
    turnout = sum(a[2] for a, o in both if o[0]) / expected if expected else 1.0
    overall = sum(a[2] for a in now.values()) / (sum(a[1] for a in now.values()) or 1)
    swing = [s / sw if sw and col else None for s, sw, col in zip(share_now, share_was, cols)]  # each list's, nationally
    # Last time's polling stations that have been counted again (a station split since keeps its number before the dot:
    # 14.1 and 14.2 were 14), and in each town, the counted stations next to themselves last time.
    stem = lambda b: b.split('.')[0]
    again, matched = set(), {}  # town -> [votes now per list, valid now, the same stations last time]
    for (t, b), n in rows.items():
        if not n[1]:
            continue
        again.update({(t, b), (t, stem(b))})
        key = (t, b) if (t, b) in p_rows else (t, stem(b)) if (t, stem(b)) in p_rows else None
        if key and p_rows[key][3] and n[3]:
            m = matched.setdefault(town(t), [[0] * len(letters), 0, {}])
            m[0] = [x + y for x, y in zip(m[0], n[4:])]
            m[1] += n[3]
            m[2][key] = p_rows[key]
    # Once enough is matched, the national swing is measured like for like too, so that a town whose counted part leans
    # one way doesn't pass for a swing.
    now_valid, past = sum(m[1] for m in matched.values()), [(n[3], then(n)) for m in matched.values() for n in m[2].values()]
    if now_valid >= 2000:
        was_valid = sum(v for v, _ in past)
        swing = [sum(m[0][j] for m in matched.values()) / now_valid / (sum(v * w[j] for v, w in past) / was_valid or 1) if col else None
                 for j, col in enumerate(cols)]
    pending = {}  # town -> last time's stations not counted yet
    for (t, b), n in p_rows.items():
        if n[3] and (t, b) not in again and (t, stem(b)) not in again:
            pending.setdefault(town(t), []).append(n)

    def predict(stations, k, level):  # how these stations would vote now: as last time, moved by the swing k
        acc, vol, rest = [0.0] * len(letters), 0, 1 - sum(level)
        for n in stations:
            g = [w * x if x is not None else 0 for w, x in zip(then(n), k)]
            if sum(g) > 0 and rest > 0:
                acc = [a + n[3] * (x * rest / sum(g) + y) for a, x, y in zip(acc, g, level)]
                vol += n[3]
        return [a / vol for a in acc] if vol else None

    votes, tally, remaining = [0.0] * len(letters), [0] * len(letters), {}
    for t in now.keys() | old.keys():
        e_all, e, v, c = now.get(t, [0, 0, 0, [0] * len(letters)])
        o = old.get(t)
        if o and not o[0]:  # double envelopes have no voters of their own: expect as many as last time, adjusted
            left = max(0.0, o[1] * growth * turnout - v)
        else:
            full = e_all if lists_all else max(e_all, (o[0] if o else 0) * growth)
            rate = v / e if e >= 1000 else o[1] / o[0] * turnout if o else overall
            left = max(0.0, full - e) * rate
        level = [(x + prior * s) / (v + prior) if not col else 0 for x, s, col in zip(c, share_now, cols)]  # new lists
        m, mix = matched.get(t), None
        if pending.get(t):
            k = swing
            if m:  # the town's own swing on its matched stations, blended with the national one
                th = [sum(n[3] * w for n, w in zip(m[2].values(), ws)) / sum(n[3] for n in m[2].values())
                      for ws in zip(*[then(n) for n in m[2].values()])]
                k = [(x / th_j + prior * g) / (m[1] + prior) if g is not None and th_j else g for x, th_j, g in zip(m[0], th, swing)]
            left_like, counted_like = predict(pending[t], k, level), predict(m[2].values(), k, level) if m else None
            if left_like and counted_like:  # trusted as far as the counted stations could be matched to last time
                mix = [max(0.0, x / v + m[1] / v * (a - b)) for x, a, b in zip(c, left_like, counted_like)]
                mix = [x / (sum(mix) or 1) for x in mix]
            elif left_like and not v:
                mix = left_like
        if mix is None:
            if o and o[1]:
                guess = [s * w / sw if sw and col else s for s, w, sw, col in zip(share_now, was(o), share_was, cols)]
                guess = [g / (sum(guess) or 1) for g in guess]
            else:
                guess = share_now
            mix = [(x + prior * g) / (v + prior) for x, g in zip(c, guess)]
        votes = [so_far + x + left * y for so_far, x, y in zip(votes, c, mix)]
        tally = [a + x for a, x in zip(tally, c)]
        if left >= 1:
            remaining[t] = (round(left), letters[max(range(len(letters)), key=mix.__getitem__)],
                            names.get(t) or p_names.get(t) or t)
    return {'votes': dict(zip(letters, map(round, votes))), 'counted': dict(zip(letters, tally)), 'remaining': remaining}


def strongholds(live, prev, before, top=100, least=100):
    """Turnout where each list is strongest: the `top` polling stations (of those with at least `least` valid votes) where
    the lists it ran on last time had their largest share. Returns letters -> [those stations counted so far, stations,
    their turnout now, the same stations' turnout last time], matched as in project, and the same over every counted
    station that could be matched, as 'all'."""
    letters, rows = live[0], live[1]
    p_letters, p_rows = prev[0], prev[1]
    stem = lambda b: b.split('.')[0]
    now = {}  # last time's station -> [eligible, voted] of the stations counted under its number now
    for (t, b), n in rows.items():
        key = (t, b) if (t, b) in p_rows else (t, stem(b)) if (t, stem(b)) in p_rows else None
        if key and n[0] and n[1] and p_rows[key][0]:
            a = now.setdefault(key, [0, 0])
            a[0] += n[0]
            a[1] += n[1]
    rate = lambda xs: sum(x[1] for x in xs) / (sum(x[0] for x in xs) or 1)
    turnout = lambda keys: [sum(k in now for k in keys), len(keys), rate([now[k] for k in keys if k in now]),
                            rate([p_rows[k] for k in keys if k in now])]
    out, pool = {'all': turnout(list(now))}, [k for k, n in p_rows.items() if n[0] and n[3] >= least]
    for l in letters:
        cols = [p_letters.index(x) for x in before.get(l, ()) if x in p_letters]
        if cols:
            out[l] = turnout(sorted(pool, key=lambda k: -sum(p_rows[k][4 + i] for i in cols) / p_rows[k][3])[:top])
    return out


ROSTER = {26: 12018}  # polling stations on the committee's list, not counting the double envelopes
projection_cache = {}


def projection(kn):
    """The live election's projection, from its latest spreadsheet and the previous election's; None without one."""
    stamps = saved(kn, 'ballots') if kn == cfg['knesset'] and cfg.get('poll_spreadsheet') else []
    prev = max((k for k in PAST if k < kn), default=None)
    if not stamps or prev is None:
        return None
    if (kn, stamps[-1]) not in projection_cache:
        before = {}
        for f in cfg.get('families', []):
            a, b = f['runs'].get(str(prev)), f['runs'].get(str(kn))
            if a and b:
                before.setdefault(b, set()).add(a)
        eligible = lambda d: next((int(v.replace(',', '')) for h, v in (d or {}).get('summary', []) if 'בעלי זכות' in h and v.replace(',', '').isdigit()), 0)
        now, then = eligible(state['national']), eligible(final_national(prev))
        growth = now / then if now and then else 1.0
        live, last = load_ballots(kn, stamps[-1]), final_ballots(prev)
        p = project(live, last, before, growth)
        if p:
            seats = allocate(p['votes'], cfg['seats'], cfg['threshold'], cfg['elections'].get(str(kn), {}).get('agreements', []))
            top = sorted(p['remaining'].items(), key=lambda r: -r[1][0])[:12]
            # Polling stations counted, out of those the file lists, or else the committee's list, or else last time's
            listed = [n for (t, _), n in live[1].items() if t not in ENVELOPES]
            done = sum(1 for n in listed if n[1])
            total = len(listed) if len(listed) > done else ROSTER.get(kn) or sum(t not in ENVELOPES for t, _ in last[1])
            p = {'time': datetime.strptime(stamps[-1], '%Y-%m-%dT%H%M%S').astimezone().isoformat(), 'previous': prev,
                 'counted': sum(n[3] for n in live[1].values()),
                 'left': sum(r[0] for r in p['remaining'].values()), 'votes': p['votes'], 'so_far': p['counted'], 'seats': seats,
                 'towns': [[t, name, n, lead] for t, (n, lead, name) in top],
                 'stations': [done, max(done, total), len(listed) > done or kn in ROSTER], 'strongholds': strongholds(live, last, before)}
        projection_cache.clear()
        projection_cache[(kn, stamps[-1])] = p
    return projection_cache[(kn, stamps[-1])]


spreadsheet = {'etag': None, 'modified': None, 'sha': None}


def check_ballots():
    """The per-polling-station spreadsheet, if the committee publishes one: one request per check, and an unchanged
    file costs almost nothing, because the server answers 304 Not Modified. Each new version is saved."""
    kn = cfg['knesset']
    headers = {h: v for h, v in (('If-None-Match', spreadsheet['etag']), ('If-Modified-Since', spreadsheet['modified'])) if v}
    r = get(f'https://media{kn}.bechirot.gov.il/files/expb.csv', headers=headers)
    if r.status_code in (304, 404):  # unchanged, or not published (yet)
        return
    r.raise_for_status()
    spreadsheet.update(etag=r.headers.get('ETag'), modified=r.headers.get('Last-Modified'))
    stamps = saved(kn, 'ballots')
    if spreadsheet['sha'] is None and stamps:  # after a restart: compare with the last version saved
        spreadsheet['sha'] = hashlib.sha256(gzip.decompress((SNAPSHOTS / f'k{kn}_ballots_{stamps[-1]}.csv.gz').read_bytes())).hexdigest()
    sha = hashlib.sha256(r.content).hexdigest()
    if sha == spreadsheet['sha']:
        return
    parse_ballots(r.content)  # refuse a file we can't read before saving it
    t = datetime.now().astimezone()
    SNAPSHOTS.mkdir(exist_ok=True)
    (SNAPSHOTS / f'k{kn}_ballots_{t:%Y-%m-%dT%H%M%S}.csv.gz').write_bytes(gzip.compress(r.content))
    spreadsheet['sha'] = sha
    state['version'] += 1
    print(f'{t.isoformat(timespec="seconds")}  new results spreadsheet', flush=True)


def can_edit(headers):
    if ADMIN_TOKEN:
        return hmac.compare_digest(headers.get('X-Admin-Token', '').encode(), ADMIN_TOKEN.encode())
    # No token: only someone on this machine may edit. Visitors arriving through a tunnel or proxy also look
    # local, but those add forwarding headers.
    return LOCAL and not (headers.get('X-Forwarded-For') or headers.get('Cf-Connecting-Ip'))


def cities(kn):
    if kn != cfg['knesset']:  # past elections: the stored spreadsheet lists every town
        return sorted(final_ballots(kn)[3].items(), key=lambda town: town[1])
    return cached(kn, 'cityresults', lambda p: [(c, town_name(c, n)) for c, n in parse_options(p, 'select1')])


def ballots(kn, city):
    if city not in dict(cities(kn)):  # only ask the site for pages that exist
        raise ValueError('unknown municipality')
    if kn != cfg['knesset']:  # past elections: from the stored spreadsheet, by station number
        numbers = sorted((b for t, b in final_ballots(kn)[1] if t == city), key=lambda b: [int(p) for p in b.split('.') if p.isdigit()])
        return [(b, f'קלפי {b}') for b in numbers]
    return cached(kn, f'ballotresults?cityID={city}', lambda p: parse_options(p, 'select2'))


def check_place(kn, city, ballot):
    if city and city not in dict(cities(kn)) or ballot and ballot not in dict(ballots(kn, city)):
        raise ValueError('unknown municipality or polling station')


def timeline_kind(kn, city, ballot):
    """Where the saved results for this view come from: the spreadsheet covers every place, when there is one."""
    if not city:
        return 'national'
    use = kn == cfg['knesset'] and cfg.get('poll_spreadsheet') and saved(kn, 'ballots')
    return 'ballots' if use else f'place_{city}_{ballot or "all"}'


def history(kn, city=None, ballot=None):
    """Every saved result for the whole country or one place, oldest first: the id to ask for, and when it arrived."""
    check_place(kn, city, ballot)
    return [{'id': s, 'time': datetime.strptime(s, '%Y-%m-%dT%H%M%S').astimezone().isoformat()}
            for s in saved(kn, timeline_kind(kn, city, ballot))]


def results(kn, city=None, ballot=None, at=None):
    check_place(kn, city, ballot)
    if at and not re.fullmatch(STAMP, at):
        raise ValueError('no results were saved at that time')
    kind = timeline_kind(kn, city, ballot)
    if kn != cfg['knesset']:  # a past election: everything is stored locally
        data = from_ballots(final_ballots(kn), city, ballot, final_national(kn)) if city else final_national(kn)
    elif kind == 'ballots':
        data = from_ballots(load_ballots(kn, at or saved(kn, 'ballots')[-1]), city, ballot)
    elif at:
        f = SNAPSHOTS / f'k{kn}_{kind}_{at}.json'
        if not f.exists():
            raise ValueError('no results were saved at that time')
        data = json.loads(f.read_text('utf-8'))
    elif city:
        path = f'ballotresults?cityID={city}&BallotNumber={ballot}' if ballot else f'cityresults?cityID={city}'
        # Each new version of a page someone opens is kept, which builds that place's timeline at no extra cost.
        record = (lambda d: save(kn, kind, d)) if kn == cfg['knesset'] else None
        data = cached(kn, path, parse_results, live=True, on_change=record)
    elif state['national']:
        data = state['national']
    else:
        raise RuntimeError('no national results yet')
    if kn == cfg['knesset'] and not city and not at:
        try:
            data = {**data, 'projection': projection(kn)}
        except Exception as e:  # a projection is a bonus: never let it stop the results
            print(f'projection: {type(e).__name__}: {e}', flush=True)
    if all('seats' in l for l in data['lists']):  # official final seats: nothing to work out
        return data
    seats = allocate({l['letters']: l['votes'] for l in data['lists']},
                     cfg['seats'], cfg['threshold'], cfg['elections'].get(str(kn), {}).get('agreements', []))
    return {**data, 'lists': [{**l, 'seats': seats[l['letters']]} for l in data['lists']]}


def load_snapshot():
    """Start from the last results saved for this election, so the page has something to show at once."""
    stamps = saved(cfg['knesset'], 'national')
    if stamps:
        stamp = stamps[-1]
        state.update(national=json.loads((SNAPSHOTS / f"k{cfg['knesset']}_national_{stamp}.json").read_text('utf-8')),
                     version=state['version'] + 1,
                     last_change=datetime.strptime(stamp, '%Y-%m-%dT%H%M%S').astimezone().isoformat())


def public_state(headers):
    sheets = saved(cfg['knesset'], 'ballots') if cfg.get('poll_spreadsheet') else []
    return {k: state[k] for k in ('version', 'status', 'last_check', 'last_change')} \
        | {'config': cfg, 'past': PAST, 'can_edit': can_edit(headers), 'sheet': sheets[-1] if sheets else None}


def monitor():
    while True:
        try:
            data = parse_results(fetch('nationalresults'))
            t = datetime.now().astimezone()
            now = t.isoformat(timespec='seconds')  # includes the UTC offset, so every browser shows its own local time
            state['last_check'] = now
            if data != state['national']:
                state.update(national=data, last_change=now, version=state['version'] + 1)
                save(cfg['knesset'], 'national', data, t)
                print(f'{now}  new results', flush=True)
            state['status'] = 'ok'
        except Exception as e:  # keep the last good results and retry next cycle
            state['status'] = f'{type(e).__name__}: {e}'
            print(datetime.now().strftime('%H:%M:%S'), state['status'], flush=True)
        try:
            if cfg.get('poll_spreadsheet'):  # off by default; the admin turns it on in Site settings
                check_ballots()
        except Exception as e:  # the spreadsheet is a bonus: never let it affect the national results
            print(datetime.now().strftime('%H:%M:%S'), 'results spreadsheet:', type(e).__name__, e, flush=True)
        wake.wait(max(30, cfg['poll_seconds']))  # the site itself caches pages for 60 s
        wake.clear()


def byte_range(header, size):
    """The part of a file a Range header asks for, as (first, last) byte; None for the whole file (no header, or one
    this doesn't read); False when it starts past the end."""
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', header or '')
    if not m or not (m[1] or m[2]) or (m[1] and m[2] and int(m[2]) < int(m[1])):
        return None
    if not m[1]:  # the last n bytes
        return max(0, size - int(m[2])), size - 1
    return (int(m[1]), min(int(m[2] or size - 1), size - 1)) if int(m[1]) < size else False


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype='application/json; charset=utf-8', headers=()):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for h in headers:
            self.send_header(*h)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == '/':  # the page arrives with the latest results already in it
                boot = json.dumps({'state': public_state(self.headers),
                                   'results': results(cfg['knesset']) if state['national'] else None},
                                  ensure_ascii=False).replace('</', '<\\/')  # results text can't close the script tag
                page = (HERE / 'index.html').read_text('utf-8').replace('<!--BOOT-->', f'<script>const BOOT = {boot};</script>')
                # Link previews (Discord) need the image's full address; a host name that isn't plain gets no image.
                host, scheme = self.headers.get('Host', ''), 'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'
                page = page.replace('content="/og.png"', f'content="{scheme}://{host}/og.png"'
                                    if re.fullmatch(r'[A-Za-z0-9.-]+(:\d+)?', host) else 'content=""')
                return self.send(200, page.encode(), 'text/html; charset=utf-8')
            if url.path == '/analysis.js':
                return self.send(200, (HERE / 'analysis.js').read_bytes(), 'text/javascript; charset=utf-8')
            if url.path == '/og.png':  # the link preview image (made once, from docs/og.html)
                return self.send(200, (HERE / 'data' / 'og.png').read_bytes(), 'image/png')
            if url.path == '/surf.mp4':  # the Subway Surfers clip, in parts when asked (Safari plays video only that way)
                body = (HERE / 'data' / 'surf.mp4').read_bytes()
                part, more = byte_range(self.headers.get('Range'), len(body)), [('Accept-Ranges', 'bytes'), ('Cache-Control', 'public, max-age=86400')]
                if part is False:
                    return self.send(416, b'', 'video/mp4', [('Content-Range', f'bytes */{len(body)}')])
                if part is None:
                    return self.send(200, body, 'video/mp4', more)
                return self.send(206, body[part[0]:part[1] + 1], 'video/mp4', more + [('Content-Range', f'bytes {part[0]}-{part[1]}/{len(body)}')])
            if url.path == '/api/candidates':  # each list's candidates in order, by election and ballot letters
                return self.send(200, (HERE / 'data' / 'candidates.json').read_bytes())
            if url.path == '/api/places':  # where each town is and what kind of place it is (build_towns.py)
                return self.send(200, (HERE / 'data' / 'towns.json').read_bytes())
            if url.path == '/api/map':  # town shapes, outlines and background for the maps (build_map.py)
                return self.send(200, (HERE / 'data' / 'map.json').read_bytes())
            if url.path == '/api/state':
                return self.send(200, public_state(self.headers))
            if url.path == '/api/cities':
                return self.send(200, cities(election(q.get('k'))))
            if url.path == '/api/ballots':
                return self.send(200, ballots(election(q.get('k')), q.get('city')))
            if url.path == '/api/results':
                return self.send(200, results(election(q.get('k')), q.get('city'), q.get('ballot'), q.get('at')))
            if url.path == '/api/towns':
                return self.send(200, towns(election(q.get('k'))))
            if url.path == '/api/stations':
                return self.send(200, stations(election(q.get('k'))))
            if url.path == '/api/history':
                return self.send(200, history(election(q.get('k')), q.get('city'), q.get('ballot')))
            self.send(404, {'error': 'not found'})
        except Exception as e:
            self.send(502, {'error': f'{type(e).__name__}: {e}'})

    def do_POST(self):  # /api/config: the admin UI saves the whole config object
        if not can_edit(self.headers):
            return self.send(403, {'error': 'Only the site admin can change shared settings'})
        new = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        switched = new.get('knesset') != cfg['knesset']
        cfg.update(new)
        CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
        if switched:
            page_cache.clear()
            spreadsheet.update(etag=None, modified=None, sha=None)
            state.update(national=None, last_change=None, version=state['version'] + 1, status='Switching election…')
            load_snapshot()
            wake.set()
        self.send(200, {'ok': True})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:  # already running: just open the UI
        if LOCAL:
            webbrowser.open(f'http://localhost:{PORT}')
        raise SystemExit
    load_snapshot()
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=download_past, daemon=True).start()
    print(f'Serving on http://{HOST}:{PORT}' + (' - close this window to stop.' if LOCAL else ''), flush=True)
    if LOCAL:
        webbrowser.open(f'http://localhost:{PORT}')
    server.serve_forever()
