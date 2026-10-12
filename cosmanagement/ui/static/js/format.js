// ---- formatting ----------------------------------------------------------------------
function fmtBytes(n) {
  if (n === null || n === undefined) return 'n/a';
  const units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
  let v = n, i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return (i === 0 ? String(v) : (Math.round(v * 100) / 100).toLocaleString('en-GB')) + ' ' + units[i];
}
function pct(used, quota) { return quota > 0 && used !== null && used !== undefined ? used / quota * 100 : null; }
function fmtPct(p) { return p === null ? 'n/a' : (Math.round(p * 10) / 10).toLocaleString('en-GB') + ' %'; }
function pctClass(p) { return p === null ? 'num' : p >= 100 ? 'num crit' : p >= 80 ? 'num warn' : 'num'; }
