"""Build data/candidates.json: each list's candidates in order, by election and ballot letters, for the names behind seats.

Usage: python build_candidates.py k26_mks.json
(k26_mks.json: the Central Elections Committee's 2026 lists, top 30 or so of each, surname first, as collected from
gov.il/he/pages/candidates-lists-26 by the קולות נודדים project: github.com/harelc/elections-vote-transfer, site/data)

2026: the committee's order and names, spelled as Hebrew Wikipedia's article on the election spells them (given name
first) wherever both name the same person, and cut at the first name that can't be turned around with confidence (well
below any realistic seat count). Lists the committee file skips come from Wikipedia alone. 2022: Hebrew Wikipedia."""
import json
import re
import sys
from pathlib import Path

import requests

ARTICLES = {'25': 'הבחירות לכנסת העשרים וחמש', '26': 'הבחירות לכנסת העשרים ושש'}
COMMITTEE = {'ישר': 'דרך', 'הליכוד': 'מחל', 'ביחד': 'רק', 'הדמוקרטים': 'אמת', 'יהדות התורה': 'ג', 'ישראל ביתנו': 'ל', 'הרשימה המשותפת': 'ודם',
             'ש״ס': 'שס', 'עוצמה יהודית': 'ב', 'הציונות הדתית': 'ט', 'רע״ם': 'עם', 'עמך ישראל': 'ך', 'המילואימניקים והכלכלית': 'די'}
WIKI_ONLY = ('ני', 'זך', 'בקר')  # 2026 lists the committee file skips
# Checked by hand: spellings the matching can't settle (ואליד אלהואשלה is "אל הואשלה" in the committee's file).
FIX = {('עם', 4): 'ואליד אלהואשלה', ('ך', 1): 'עופר וינטר', ('ב', 9): 'יוסי גולדברגר', ('ודם', 25): 'נועה לוי', ('אמת', 12): 'אבי דבוש'}
PREFIX = {'בן', 'אבו', 'אל', 'בר', 'דה', 'אבן'}  # parts of a surname: אבו רוקן כמיל is כמיל אבו רוקן


def wikitext(title):
    r = requests.get('https://he.wikipedia.org/w/api.php', params={'action': 'parse', 'page': title, 'prop': 'wikitext', 'format': 'json'},
                     headers={'User-Agent': 'knesset-live-count/1.0 (build_candidates.py)'}, timeout=60)
    r.raise_for_status()
    return r.json()['parse']['wikitext']['*']


def clean(s):
    s = re.sub(r'<ref[^>]*/>|<ref[^>]*>.*?</ref>|\{\{(?:הערה|efn|refn)\|.*?\}\}', '', s)
    s = re.sub(r'<small>.*?</small>|<sup>.*?</sup>|\(.*?\)', '', s)
    s = re.sub(r'\{\{(?:טקסט מחוק|s|strike)\|(.*?)\}\}', r'\1', s)
    s = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', s)
    s = re.sub(r"'''?|\{\{[^}]*\}\}|<[^>]+>|⁠", '', s)
    return re.sub(r'\s+', ' ', s).strip(' -–,*')


def lists(text):
    """Ballot letters -> the numbered names in that list's section. A struck-out list (withdrawn) is left out."""
    names, letters, head = {}, {}, None
    for line in text.splitlines():
        m = re.match(r'^(=+)\s*(.*?)\s*\1\s*$', line)
        if m:
            head = clean(m.group(2))
            continue
        for struck, x in re.findall(r'(\{\{טקסט מחוק\|)?\{\{אותיות רשימה\|([^|}]+)', line):
            if not struck:
                letters[x] = head
        m = re.match(r'^(?:\|\s*תוכן\s*=\s*)?#\s*(.+)', line)
        if head and m and not line.startswith('#:') and clean(m.group(1)):
            names.setdefault(head, []).append(clean(m.group(1)))
    return {x: names[h] for x, h in letters.items() if names.get(h)}


FINAL = str.maketrans('ךםןףץ', 'כמנפצ')
words = lambda s: [w.translate(FINAL) for w in re.sub('[\'"״׳`*]', '', s).replace('-', ' ').replace('־', ' ').split()]


def alike(a, b):  # one word, spelled a little differently (גולדקנופף, גולדקנופ)
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and abs(len(a) - len(b)) <= 1 and (a.startswith(b) or b.startswith(a)):
        return True
    return min(len(a), len(b)) >= 4 and len(a) == len(b) and sum(x != y for x, y in zip(a, b)) <= 1


def same(wiki, committee):
    h, t = words(wiki), words(committee)
    hit = [any(alike(x, y) for y in t) for x in h]
    # every word of Wikipedia's name, or the surname and the start of a nickname (רפי for רפאל)
    return all(hit) or (len(h) == 2 and h[1] in t and h[0][:2] == next((y for y in t if y != h[1]), '')[:2])


def turned(name):
    """Surname first to given name first, when the split is clear; None when it isn't."""
    w = name.split()
    if len(w) == 2:
        return f'{w[1]} {w[0]}'
    if len(w) == 3 and w[0] in PREFIX:
        return f'{w[2]} {w[0]} {w[1]}'
    return None


def main(harvest):
    committee = json.loads(Path(harvest).read_text('utf-8'))['parties']
    wiki = {k: lists(wikitext(t)) for k, t in ARTICLES.items()}
    out = {'25': wiki['25'], '26': {}}
    for party, letters in COMMITTEE.items():
        w, names = wiki['26'].get(letters, []), []
        for i, c in enumerate(committee[party]):
            near = sorted(range(max(0, i - 3), min(len(w), i + 4)), key=lambda j: abs(j - i))
            j = next((j for j in near if same(w[j], c)), None)
            if j is None and i < len(w) and any(alike(words(w[i])[-1], y) for y in words(c)):  # same place, same surname
                j = i
            name = FIX.get((letters, i + 1)) or (w[j] if j is not None else turned(c))
            if name is None:
                break
            names.append(name)
        if len(names) == len(committee[party]) and all(same(a, b) or (letters, i + 1) in FIX for i, (a, b) in enumerate(zip(w, names))):
            names += w[len(names):]  # Wikipedia agrees all the way, and goes further (the committee file has 21 of Likud's)
        out['26'][letters] = names
    for letters in WIKI_ONLY:
        out['26'][letters] = wiki['26'][letters]
    path = Path(__file__).resolve().parent / 'data' / 'candidates.json'
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(',', ':')), 'utf-8')
    print({k: {x: len(v) for x, v in d.items()} for k, d in out.items()})


if __name__ == '__main__':
    main(sys.argv[1])
