// Calculations the page shares with its tests (node test_analysis.js): seats, how close each seat was, and vote flows.
(function (exports) {
  const sum = a => a.reduce((t, x) => t + x, 0);

  // D'Hondt: each seat goes to the highest votes / (seats won + 1); a tie goes to the list named first.
  function dhondt(votes, seats) {
    const keys = Object.keys(votes), won = Object.fromEntries(keys.map(k => [k, 0]));
    for (let s = 0; keys.length && s < seats; s++) {
      let best = keys[0];
      for (const k of keys) if (votes[k] / (won[k] + 1) > votes[best] / (won[best] + 1)) best = k;
      won[best]++;
    }
    return won;
  }

  // Bader-Ofer, exactly as app.py's allocate: lists under the threshold drop out, each surplus-agreement pair whose
  // lists both pass counts as one list, and a pair's seats are then split between its two lists the same way.
  function allocate(votes, { seats = 120, threshold = 0.0325, agreements = [] } = {}) {
    const total = sum(Object.values(votes));
    const passed = Object.fromEntries(Object.entries(votes).filter(([, v]) => v && v >= threshold * total));
    const group = Object.fromEntries(Object.keys(passed).map(k => [k, [k]]));
    for (const [a, b] of agreements)
      if (a !== b && group[a]?.length === 1 && group[b]?.length === 1) group[a] = group[b] = [a, b];
    const pairs = {};
    for (const k in passed) pairs[group[k].join('|')] ??= sum(group[k].map(x => passed[x]));
    const result = Object.fromEntries(Object.keys(votes).map(k => [k, 0]));
    for (const [g, n] of Object.entries(dhondt(pairs, seats)))
      Object.assign(result, dhondt(Object.fromEntries(g.split('|').map(x => [x, passed[x]])), n));
    return result;
  }

  // For each list, with everyone else's votes unchanged: how many more votes it needed for one more seat (for a list
  // under the threshold, that is the votes to cross it), and how many it could lose before losing a seat.
  function margins(votes, opts) {
    const base = allocate(votes, opts), out = {};
    for (const [k, v] of Object.entries(votes)) {
      const seats = d => allocate({ ...votes, [k]: Math.max(0, v + d) }, opts)[k];
      // smallest change that flips the seat count, by doubling then halving: seat counts only grow with votes
      const edge = (dir, flipped) => {
        let lo = 0, hi = 1;
        while (!flipped(seats(dir * hi))) { lo = hi; hi *= 2; if (hi > 1e9) return null; }
        while (hi - lo > 1) { const m = Math.floor((lo + hi) / 2); if (flipped(seats(dir * m))) hi = m; else lo = m; }
        return hi;
      };
      out[k] = { seats: base[k], gain: edge(1, s => s > base[k]), lose: base[k] ? edge(-1, s => s < base[k]) : null };
    }
    return out;
  }

  // What each surplus agreement did: seats with all agreements, minus seats with that one agreement left out.
  function agreementEffects(votes, opts) {
    const base = allocate(votes, opts);
    return (opts.agreements || []).map(pair => {
      const without = allocate(votes, { ...opts, agreements: opts.agreements.filter(p => p !== pair) });
      return { pair, change: Object.fromEntries(Object.keys(base).filter(k => base[k] !== without[k]).map(k => [k, base[k] - without[k]])) };
    });
  }

  // Ecological estimate of how voters moved between two elections, from town totals alone. X[t][i] is town t's
  // eligible voters by earlier choice (each list, plus not voting), Y[t][j] the same for the later election. Assuming
  // the share of earlier choice i that moved to j is the same in every town, the EM algorithm finds the shares that best
  // explain every town's later result. Returns B[i][j], each row adding up to 1. It is an estimate, not a survey.
  function flows(X, Y, iterations = 300) {
    const I = X[0].length, J = Y[0].length, T = X.length;
    const totY = Array(J).fill(0);
    Y.forEach(y => y.forEach((v, j) => totY[j] += v));
    const all = sum(totY) || 1;
    let B = Float64Array.from({ length: I * J }, (_, n) => totY[n % J] / all);  // start: everyone votes like the country
    const px = new Float64Array(I), p = new Float64Array(J);
    for (let it = 0; it < iterations; it++) {
      const N = new Float64Array(I * J);
      for (let t = 0; t < T; t++) {
        const x = X[t], y = Y[t], nx = sum(x);
        if (!nx || !sum(y)) continue;
        for (let i = 0; i < I; i++) px[i] = x[i] / nx;
        p.fill(0);
        for (let i = 0; i < I; i++) if (px[i]) for (let j = 0; j < J; j++) p[j] += px[i] * B[i * J + j];
        for (let j = 0; j < J; j++) {
          if (!y[j] || !p[j]) continue;
          const f = y[j] / p[j];
          for (let i = 0; i < I; i++) if (px[i]) N[i * J + j] += px[i] * B[i * J + j] * f;
        }
      }
      for (let i = 0; i < I; i++) {
        let r = 0;
        for (let j = 0; j < J; j++) r += N[i * J + j];
        if (r) for (let j = 0; j < J; j++) N[i * J + j] /= r; else for (let j = 0; j < J; j++) N[i * J + j] = B[i * J + j];
      }
      B = N;
    }
    return Array.from({ length: I }, (_, i) => Array.from(B.slice(i * J, i * J + J)));
  }

  // Lists of two elections joined into comparable groups: two lists are in one group if a party ran on both (a merger,
  // split or rename), following runs = [{earlier letters, later letters}, ...]. Returns letters -> group id per side.
  function lineage(earlier, later, runs) {
    const parent = {}, find = x => parent[x] === x ? x : (parent[x] = find(parent[x]));
    earlier.forEach(x => parent['a' + x] = 'a' + x);
    later.forEach(x => parent['b' + x] = 'b' + x);
    for (const [a, b] of runs) if (('a' + a) in parent && ('b' + b) in parent) parent[find('a' + a)] = find('b' + b);
    return { a: Object.fromEntries(earlier.map(x => [x, find('a' + x)])), b: Object.fromEntries(later.map(x => [x, find('b' + x)])) };
  }

  // Pedersen volatility: half the total change in vote shares between two elections, over groups of comparable lists.
  function volatility(sharesA, sharesB, groups) {
    const g = {};
    for (const [x, s] of Object.entries(sharesA)) g[groups.a[x]] = (g[groups.a[x]] || 0) - s;
    for (const [x, s] of Object.entries(sharesB)) g[groups.b[x]] = (g[groups.b[x]] || 0) + s;
    return sum(Object.values(g).map(Math.abs)) / 2;
  }

  // Election night: the projection's votes still to count, drawn again and again. Their split between the lists comes
  // from a Dirichlet around the projected split and their total moves up or down; both spreads depend on the share
  // counted (x), and were fitted by replaying the 2019-2022 counts through app.project in random, town-by-town and
  // skewed orders (also with half the stations renumbered, as between 2022 and 2026), so that the 5-95% range of the
  // draws held the final seats about 9 times in 10 (87-97%, depending on how much was counted). Seeded (mulberry32),
  // so every visitor sees the same ranges. Returns one seat allocation per draw.
  const rng = seed => () => { seed = (seed + 0x6D2B79F5) >>> 0; let t = Math.imul(seed ^ (seed >>> 15), 1 | seed); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  const normal = r => Math.sqrt(-2 * Math.log(1 - r())) * Math.cos(2 * Math.PI * r());
  function gamma(k, r) {  // Marsaglia and Tsang
    if (k < 1) return gamma(k + 1, r) * r() ** (1 / k);
    const d = k - 1 / 3, c = 1 / Math.sqrt(9 * d);
    for (;;) {
      let x, v;
      do { x = normal(r); v = 1 + c * x; } while (v <= 0);
      v = v ** 3;
      const u = r();
      if (u < 1 - 0.0331 * x ** 4 || Math.log(u) < x * x / 2 + d * (1 - v + Math.log(v))) return d * v;
    }
  }
  const along = knots => x => { const i = knots.findIndex(k => k[0] >= x); if (i <= 0) return knots[Math.max(0, i)][1]; const [a, p] = knots[i - 1], [b, q] = knots[i]; return p + (q - p) * (x - a) / (b - a); };
  const SPLIT = along([[0, 3e-4], [0.15, 1.2e-4], [0.3, 1e-4], [0.8, 2e-4], [1, 2e-4]]), TOTAL = along([[0, 0.012], [0.15, 0.01], [0.5, 0.012], [0.8, 0.025], [0.93, 0.06], [1, 0.06]]);
  function simulate(counted, projected, opts, { draws = 600, seed = 26 } = {}) {
    const keys = Object.keys(projected), r = rng(seed);
    const left = keys.map(k => Math.max(0, projected[k] - (counted[k] || 0))), L = sum(left), C = sum(keys.map(k => counted[k] || 0));
    const x = C / ((C + L) || 1), split = SPLIT(x), total = TOTAL(x), out = [];
    for (let d = 0; d < draws; d++) {
      const alpha = 1 / (split * Math.exp(normal(r))) - 1;  // a spread of spreads: some nights are harder than others
      const g = left.map(v => v ? gamma(alpha * v / L, r) : 0), G = sum(g) || 1, T = L * Math.max(0, 1 + total * normal(r));
      out.push(allocate(Object.fromEntries(keys.map((k, i) => [k, (counted[k] || 0) + T * g[i] / G])), opts));
    }
    return out;
  }

  Object.assign(exports, { allocate, margins, agreementEffects, flows, lineage, volatility, simulate });
})(typeof module === 'object' ? module.exports : (window.Analysis = {}));
