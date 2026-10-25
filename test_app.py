"""Offline self-check of the seat allocation: python test_app.py"""
import json
import tempfile
from pathlib import Path

import app
from app import allocate, parse_results

# Official 2022 (25th Knesset) national results; "other" lumps the 28 lists far below the threshold.
votes = {'מחל': 1115336, 'פה': 847435, 'ט': 516470, 'כן': 432482, 'שס': 392964, 'ג': 280194, 'ל': 213687,
         'עם': 194047, 'ום': 178735, 'אמת': 175992, 'מרצ': 150793, 'ד': 138617, 'ב': 56775}
votes['other'] = 4764742 - sum(votes.values())
official = {'מחל': 32, 'פה': 24, 'ט': 14, 'כן': 12, 'שס': 11, 'ג': 7, 'ל': 6, 'עם': 5, 'ום': 5, 'אמת': 4}
pairs_2022 = [('מחל', 'ט'), ('פה', 'כן'), ('שס', 'ג'), ('אמת', 'מרצ')]
got = {k: v for k, v in allocate(votes, agreements=pairs_2022).items() if v}
assert got == official, got

# A pair wins the seat neither list would win alone, and it goes to the partner with the higher votes/(seats+1).
toy = {'A': 710, 'B': 150, 'C': 140}
assert allocate(toy, seats=10, threshold=0) == {'A': 8, 'B': 1, 'C': 1}
assert allocate(toy, seats=10, threshold=0, agreements=[('B', 'C')]) == {'A': 7, 'B': 2, 'C': 1}
# A pair is void if either list misses the threshold; a list can't be in two pairs.
assert allocate({'A': 800, 'B': 150, 'C': 50}, seats=10, threshold=0.1, agreements=[('B', 'C')]) == {'A': 9, 'B': 1, 'C': 0}
assert allocate(toy, seats=10, threshold=0, agreements=[('B', 'C'), ('A', 'C')])['A'] == 7

page = '''<table class="ResultsSummaryTable"><tr><th>סה"כ בעלי זכות בחירה</th></tr><tr><td> 5,094</td></tr></table>
<table class="TableData"><tbody><tr><td class="th" title="חד"ש תע"ל בראשות">חד"ש</td><td> ום</td><td>5</td>
<td class="Last"><div>1,263</div></td></tr></tbody></table><h3 class="LastUpdate">חלקי‏</h3>'''
assert parse_results(page) == {'summary': [['סה"כ בעלי זכות בחירה', '5,094']], 'status': 'חלקי',
                               'lists': [{'letters': 'ום', 'name': 'חד"ש תע"ל בראשות', 'votes': 1263}]}
# A restart picks up the newest saved results for the selected election, and only that election.
with tempfile.TemporaryDirectory() as d:
    app.SNAPSHOTS, k = Path(d), app.cfg['knesset']
    for name, status in [(f'k{k}_national_2026-10-27T220000', 'old'), (f'k{k}_national_2026-10-27T231500', 'new'),
                         (f'k{k - 1}_national_2026-10-28T010000', 'other election')]:
        (app.SNAPSHOTS / f'{name}.json').write_text(json.dumps({'summary': [], 'lists': [], 'status': status}))
    app.load_snapshot()
    assert app.state['national']['status'] == 'new' and app.state['last_change'].startswith('2026-10-27T23:15:00+'), app.state

# Run-together committee names get the bureau's spelling; a different name, or a code the bureau lacks, stays as it is.
assert app.town_name('978', 'כעביהטבאשחגאגרה') == "כעביה-טבאש-חג'אג'רה"
assert app.town_name('978', 'שם אחר') == 'שם אחר' and app.town_name('9999', 'מעטפות חיצוניות') == 'מעטפות חיצוניות'

print('all checks passed')

# Final results pages carry the official seats, which are used as they are instead of being recalculated.
final = page.replace('<tbody>', '<thead><tr><th>שם הרשימה</th><th>אותיות</th><th>מנדטים</th><th>קולות</th></tr></thead><tbody>')
assert parse_results(final.replace('<td>5</td>', '<td>120</td>'))['lists'] == [{'letters': 'ום', 'name': 'חד"ש תע"ל בראשות', 'votes': 1263, 'seats': 120}]
app.state['national'], app.cfg['knesset'] = parse_results(final.replace('<td>5</td>', '<td>120</td>')), 26
assert app.results(26)['lists'][0]['seats'] == 120
# A seat column that doesn't fill the Knesset, or holds something else, is ignored and the seats are worked out.
assert all('seats' not in l for l in parse_results(final)['lists'])
assert all('seats' not in l for l in parse_results(final.replace('<td>5</td>', '<td>24.19%</td>'))['lists'])
print('official seats checks passed')

# Every saved result can be looked at again, by the id the history lists; anything else is refused.
with tempfile.TemporaryDirectory() as d:
    app.SNAPSHOTS, app.cfg['knesset'] = Path(d), 26
    for stamp, votes in [('2026-10-27T220500', 100), ('2026-10-27T231500', 900)]:
        snap = {'summary': [], 'status': '', 'lists': [{'letters': 'א', 'name': 'א', 'votes': votes}]}
        (app.SNAPSHOTS / f'k26_national_{stamp}.json').write_text(json.dumps(snap))
    h = app.history(26)
    assert [x['id'] for x in h] == ['2026-10-27T220500', '2026-10-27T231500'] and h[0]['time'].startswith('2026-10-27T22:05:00')
    assert app.results(26, at='2026-10-27T220500')['lists'][0] == {'letters': 'א', 'name': 'א', 'votes': 100, 'seats': 120}
    for bad in ('2026-10-27T999999', '../config', '2026-10-27T220500.json'):
        try:
            app.results(26, at=bad)
            raise AssertionError(f'accepted {bad}')
        except ValueError:
            pass
print('history checks passed')

# Every past election has its surplus agreements set up.
assert all(app.cfg['elections'][str(k)]['agreements'] for k in app.PAST)

# The per-polling-station spreadsheet: towns are the sum of their polling stations, and the admin toggle decides
# whether towns and stations come from it (with its saved versions as their timeline) or from their own pages.
sheet = ('﻿סמל ועדה,ברזל,שם ישוב,סמל ישוב,קלפי,ריכוז,שופט,בזב,מצביעים,פסולים,כשרים,א,ב\n'
         '1,1,עיר,7,1,1,0,100,60,2,58,40,18\n1,2,עיר,7,2.1,1,0,50,30,0,30,10,20\n').encode()
letters, stations, towns, town_names = app.parse_ballots(sheet)
assert town_names == {'7': 'עיר'}
assert letters == ['א', 'ב'] and towns['7'] == [150, 90, 2, 88, 50, 38] and stations[('7', '2.1')][4:] == [10, 20]
with tempfile.TemporaryDirectory() as d:
    app.SNAPSHOTS, app.cfg['knesset'] = Path(d), 26
    app.cities = lambda kn: [('7', 'עיר')]
    app.ballots = lambda kn, city: [('1', 'קלפי 1'), ('2.1', 'קלפי 2.1')]
    (app.SNAPSHOTS / 'k26_ballots_2026-10-27T230000.csv.gz').write_bytes(__import__('gzip').compress(sheet))
    app.cfg['poll_spreadsheet'] = True
    assert app.timeline_kind(26, '7', None) == 'ballots' and [h['id'] for h in app.history(26, '7')] == ['2026-10-27T230000']
    town = app.results(26, '7', at='2026-10-27T230000')
    assert {l['letters']: (l['votes'], l['seats']) for l in town['lists']} == {'א': (50, 68), 'ב': (38, 52)}
    assert ['שיעור ההצבעה', '60.00%'] in town['summary']
    app.cfg['poll_spreadsheet'] = False  # off: towns come from their pages, and each new version of a page is kept
    pages = iter([page, page, page.replace('1,263', '1,300')])
    app.fetch = lambda path, kn=None: next(pages)
    for version in (1, 2, 3):
        app.state['version'] = version
        app.results(26, '7', '2.1')
    assert app.timeline_kind(26, '7', '2.1') == 'place_7_2.1' and len(app.saved(26, 'place_7_2.1')) >= 1
    assert [l['votes'] for l in app.results(26, '7', '2.1')['lists']] == [1300]
print('spreadsheet and place timeline checks passed')

# A past election's spreadsheet is downloaded once and kept; the statistics page gets every polling station from it.
with tempfile.TemporaryDirectory() as d:
    app.SNAPSHOTS, app.cfg['knesset'] = Path(d), 26
    downloads = []
    class Response:
        status_code, content = 200, sheet
        def raise_for_status(self): pass
    app.get = lambda url, **kw: downloads.append(url) or Response()
    app.results = lambda kn, *a, **kw: {'lists': [{'letters': 'א', 'name': 'רשימה א'}]}
    data = json.loads(app.stations(25))
    json.loads(app.stations(25))
    assert downloads == ['https://media25.bechirot.gov.il/files/expb.csv'] and data['time'] is None
    assert data['towns'] == {'7': 'עיר'} and data['names'] == {'א': 'רשימה א'} and data['letters'] == ['א', 'ב']
    assert sorted(data['stations']) == [['7', '1', 100, 60, 2, 58, 40, 18], ['7', '2.1', 50, 30, 0, 30, 10, 20]]
    try:  # the live election has no statistics until the spreadsheet is checked and published
        app.stations(26)
        raise AssertionError('live statistics without a spreadsheet')
    except LookupError:
        pass
print('statistics data checks passed')

# Past elections are served entirely from stored files: towns, polling stations and their results come from the final
# spreadsheet (older ones use the Windows Hebrew encoding and, in April 2019, a different polling station column).
import importlib
app = importlib.reload(app)
with tempfile.TemporaryDirectory() as d:
    app.SNAPSHOTS = Path(d)
    old_sheet = ('שם ישוב,סמל ישוב,מספר קלפי,בזב,מצביעים,פסולים,כשרים,א,ב\n'
                 'עיר,7,10,100,60,2,58,40,18\nעיר,7,2.1,50,30,0,30,10,20\nכפר,8,1,20,10,0,10,10,0\n').encode('cp1255')
    (app.SNAPSHOTS / 'k21_ballots_final.csv.gz').write_bytes(__import__('gzip').compress(old_sheet))
    national = {'summary': [], 'status': '', 'lists': [{'letters': 'א', 'name': 'רשימה א', 'votes': 50, 'seats': 70},
                                                       {'letters': 'ב', 'name': 'רשימה ב', 'votes': 38, 'seats': 50}]}
    (app.SNAPSHOTS / 'k21_national_final.json').write_text(json.dumps(national), 'utf-8')
    app.get = app.fetch = lambda *a, **kw: (_ for _ in ()).throw(AssertionError('past elections must not go online'))
    assert app.cities(21) == [('8', 'כפר'), ('7', 'עיר')]
    assert app.ballots(21, '7') == [('2.1', 'קלפי 2.1'), ('10', 'קלפי 10')]
    assert app.results(21)['lists'] == national['lists']
    town = app.results(21, '7')
    assert [(l['name'], l['votes']) for l in town['lists']] == [('רשימה א', 50), ('רשימה ב', 38)] and sum(l['seats'] for l in town['lists']) == 120
    assert [l['votes'] for l in app.results(21, '7', '2.1')['lists']] == [10, 20]
    # The trends page gets every town's totals from the same stored spreadsheet.
    assert json.loads(app.towns(21)) == {'letters': ['א', 'ב'], 'towns': {'7': ['עיר', 150, 90, 2, 88, 50, 38], '8': ['כפר', 20, 10, 0, 10, 10, 0]}}
print('past election storage checks passed')

# Some years end the spreadsheet header (and rows) with a stray comma: that blank column isn't a list.
stray = 'סמל ישוב,קלפי,בזב,מצביעים,פסולים,כשרים,א,ב,\n7,1,100,60,2,58,40,18,\n'.encode('cp1255')
assert app.parse_ballots(stray)[0] == ['א', 'ב'] and app.parse_ballots(stray)[1][('7', '1')] == [100, 60, 2, 58, 40, 18]
print('stray column check passed')

# Election night projection, worked by hand: town 1 is half counted, town 2 and the double envelopes not at all.
# List A2 is the old list A under a new name. Turnout so far matches last time, and A is flat where counted, so
# town 1 stays 60/40, town 2 keeps last time's lean (A 0.6*0.2/0.667 vs B 0.4*0.8/0.333, normalised) and so do envelopes.
live = (['A2', 'B'], {('1', '1'): [500, 300, 0, 300, 180, 120], ('1', '2'): [500, 0, 0, 0, 0, 0], ('2', '1'): [1000, 0, 0, 0, 0, 0]},
        {}, {'1': 'one', '2': 'two'})
prev = (['A', 'B'], {}, {'1': [1000, 610, 10, 600, 400, 200], '2': [1000, 500, 0, 500, 100, 400], '9999': [0, 100, 0, 100, 50, 50]},
        {'9999': 'envelopes'})
p = app.project(live, prev, {'A2': {'A'}, 'B': {'B'}})
assert p == {'votes': {'A2': 482, 'B': 718}, 'counted': {'A2': 180, 'B': 120}, 'remaining': {'1': (300, 'A2', 'one'), '2': (500, 'B', 'two'), '9999': (100, 'B', 'envelopes')}}, p
assert app.project((['A2', 'B'], {('1', '1'): [500, 0, 0, 0, 0, 0]}, {}, {}), prev, {}) is None  # nothing counted yet
# Station by station: the counted half of town 1 voted as last time (80% A); the other half voted 20% A last time, so it's
# expected to again, not to vote like the counted half (which would make it 4,800 A).
then = (['A', 'B'], {('1', '1'): [5000, 3000, 0, 3000, 2400, 600], ('1', '2'): [5000, 3000, 0, 3000, 600, 2400]},
        {'1': [10000, 6000, 0, 6000, 3000, 3000]}, {})
half = (['A2', 'B'], {('1', '1'): [5000, 3000, 0, 3000, 2400, 600], ('1', '2'): [5000, 0, 0, 0, 0, 0]}, {}, {'1': 'one'})
assert app.project(half, then, {'A2': {'A'}, 'B': {'B'}})['votes'] == {'A2': 3000, 'B': 3000}
# Turnout where each list is strongest: A's best station is 1 (70% voted, 60% last time); B's is 2, now split in two,
# with 2.1 counted (80%) and 2.2 not.
split = (['A2', 'B'], {('1', '1'): [5000, 3500, 0, 3500, 2800, 700], ('1', '2.1'): [2500, 2000, 0, 2000, 400, 1600],
                       ('1', '2.2'): [2500, 0, 0, 0, 0, 0]}, {}, {})
s = app.strongholds(split, then, {'A2': {'A'}, 'B': {'B'}}, top=1, least=0)
assert s == {'all': [2, 2, 5500 / 7500, 0.6], 'A2': [1, 1, 0.7, 0.6], 'B': [1, 1, 0.8, 0.6]}, s
print('projection check passed')

# Byte ranges, for the video: a start, a start and end (clamped to the file), the last n bytes, past the end, nonsense.
assert app.byte_range('bytes=0-', 100) == (0, 99) and app.byte_range('bytes=10-19', 100) == (10, 19)
assert app.byte_range('bytes=90-500', 100) == (90, 99) and app.byte_range('bytes=-30', 100) == (70, 99)
assert app.byte_range('bytes=100-', 100) is False and app.byte_range(None, 100) is None
assert app.byte_range('bytes=5-2', 100) is None and app.byte_range('bytes=-', 100) is None and app.byte_range('items=0-1', 100) is None
print('byte range check passed')
