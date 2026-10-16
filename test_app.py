"""Offline self-check of the seat allocation: python test_app.py"""
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
assert parse_results(page) == {'summary': [('סה"כ בעלי זכות בחירה', '5,094')], 'status': 'חלקי',
                               'lists': [{'letters': 'ום', 'name': 'חד"ש תע"ל בראשות', 'votes': 1263}]}
print('all checks passed')
