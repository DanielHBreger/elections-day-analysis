// Offline self-check of analysis.js: node test_analysis.js
const assert = require('assert');
const { allocate, margins, agreementEffects, flows, lineage, volatility, simulate } = require('./analysis.js');

// Same cases as test_app.py: the official 2022 seats, and toy surplus agreements.
const votes = { 'מחל': 1115336, 'פה': 847435, 'ט': 516470, 'כן': 432482, 'שס': 392964, 'ג': 280194, 'ל': 213687,
  'עם': 194047, 'ום': 178735, 'אמת': 175992, 'מרצ': 150793, 'ד': 138617, 'ב': 56775 };
votes.other = 4764742 - Object.values(votes).reduce((a, b) => a + b, 0);
const pairs = [['מחל', 'ט'], ['פה', 'כן'], ['שס', 'ג'], ['אמת', 'מרצ']];
const got = Object.fromEntries(Object.entries(allocate(votes, { agreements: pairs })).filter(([, s]) => s));
assert.deepStrictEqual(got, { 'מחל': 32, 'פה': 24, 'ט': 14, 'כן': 12, 'שס': 11, 'ג': 7, 'ל': 6, 'עם': 5, 'ום': 5, 'אמת': 4 });
const toy = { A: 710, B: 150, C: 140 }, ten = { seats: 10, threshold: 0 };
assert.deepStrictEqual(allocate(toy, ten), { A: 8, B: 1, C: 1 });
assert.deepStrictEqual(allocate(toy, { ...ten, agreements: [['B', 'C']] }), { A: 7, B: 2, C: 1 });
assert.deepStrictEqual(allocate({ A: 800, B: 150, C: 50 }, { seats: 10, threshold: 0.1, agreements: [['B', 'C']] }), { A: 9, B: 1, C: 0 });
assert.strictEqual(allocate(toy, { ...ten, agreements: [['B', 'C'], ['A', 'C']] }).A, 7);

// Margins, worked by hand: B's second seat needs B/2 > 710/8, so 178 votes (28 more); it keeps its seat while B > 710/9.
assert.deepStrictEqual(margins(toy, ten).B, { seats: 1, gain: 28, lose: 72 });
// Under the threshold, the gain is the distance to it (the threshold grows with the votes added); 2022's Jewish Home (1.2%) missed it.
const opts = { agreements: pairs }, m = margins(votes, opts), need = t => Math.ceil((0.0325 * t - votes['ב']) / (1 - 0.0325));
const total = Object.values(votes).reduce((a, b) => a + b, 0);
assert.strictEqual(m['ב'].gain, need(total));
for (const [k, x] of Object.entries(m)) {  // each margin is the exact edge: one vote less doesn't flip the seat count
  const at = d => allocate({ ...votes, [k]: votes[k] + d }, opts)[k];
  assert.ok(at(x.gain) > x.seats && at(x.gain - 1) === x.seats, k);
  if (x.lose != null) assert.ok(at(-x.lose) < x.seats && at(-x.lose + 1) === x.seats, k);
}

// The 2022 Likud and Religious Zionism pair: with it, the bloc's seats differ from without it.
const fx = agreementEffects(votes, opts);
assert.strictEqual(fx.length, 4);
for (const f of fx) assert.strictEqual(Object.values(f.change).reduce((a, b) => a + b, 0), 0);  // seats move, none appear

// Vote flows: towns made from a known flow matrix give it back.
const B = [[0.8, 0.1, 0.1], [0.2, 0.7, 0.1], [0.1, 0.2, 0.7]], X = [], Y = [];
for (let t = 0; t < 300; t++) {
  const x = [1 + (t * 37) % 97, 1 + (t * 53) % 89, 1 + (t * 71) % 83].map(v => v * 10);
  X.push(x); Y.push([0, 1, 2].map(j => x.reduce((s, v, i) => s + v * B[i][j], 0)));
}
flows(X, Y, 2000).forEach((row, i) => row.forEach((v, j) => assert.ok(Math.abs(v - B[i][j]) < 0.02, `flow ${i}->${j}: ${v}`)));

// Volatility over merged lists: a merger alone isn't a change of mind.
const g = lineage(['A', 'B', 'C'], ['AB', 'C'], [['A', 'AB'], ['B', 'AB'], ['C', 'C']]);
assert.strictEqual(g.a.A, g.b.AB); assert.strictEqual(g.a.B, g.b.AB);
assert.strictEqual(volatility({ A: 0.3, B: 0.2, C: 0.5 }, { AB: 0.5, C: 0.5 }, g), 0);
assert.ok(Math.abs(volatility({ A: 0.3, B: 0.2, C: 0.5 }, { AB: 0.4, C: 0.6 }, g) - 0.1) < 1e-12);

// Seat ranges: every draw fills the Knesset; nothing left to count means no doubt; the same seed gives the same draws;
// with half the 2022 votes still out, the ranges hold the official seats and the threshold lists' fate is uncertain.
const half = Object.fromEntries(Object.entries(votes).map(([k, v]) => [k, Math.round(v / 2)]));
const sims = simulate(half, votes, opts, { draws: 400 });
assert.ok(sims.every(s => Object.values(s).reduce((a, b) => a + b, 0) === 120));
assert.ok(simulate(votes, votes, opts, { draws: 20 }).every(s => JSON.stringify(s) === JSON.stringify(allocate(votes, opts))));
assert.deepStrictEqual(simulate(half, votes, opts, { draws: 5 }), sims.slice(0, 5));
const seatsOf = k => sims.map(s => s[k]).sort((a, b) => a - b), official = allocate(votes, opts);
for (const k of ['מחל', 'פה', 'ט', 'כן', 'שס']) assert.ok(seatsOf(k)[20] <= official[k] && official[k] <= seatsOf(k)[379], k);
const passes = k => sims.filter(s => s[k]).length / sims.length;
assert.ok(passes('מרצ') > 0.05 && passes('מרצ') < 0.95 && passes('מחל') === 1, passes('מרצ'));

console.log('all checks passed');
