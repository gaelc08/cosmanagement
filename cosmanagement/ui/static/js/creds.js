// ---- credentials panel ----------------------------------------------------
function copyText(text, label) {
  navigator.clipboard.writeText(text).then(
    () => setStatus(label + ' copied.', false),
    () => setStatus('Cannot copy (clipboard access denied).', true));
}

function showCreds(list) {
  const box = $('creds');
  box.textContent = '';
  box.append(el('strong', 'Credentials (shown here only, nothing is written to disk)'));
  for (const c of list) {
    const wrap = el('div', undefined, 'cred');
    wrap.append(el('div', c.account, 'mono'));

    const r1 = el('div', undefined, 'row');
    r1.append(el('span', 'Access key ID', 'k'), el('span', c.access_key_id, 'v'));
    const c1 = el('button', 'Copy', 'small secondary');
    c1.addEventListener('click', () => copyText(c.access_key_id, 'Access key ID'));
    r1.append(c1);

    const r2 = el('div', undefined, 'row');
    const secret = el('span', '••••••••••••', 'v');
    let shown = false;
    const reveal = el('button', 'Show', 'small secondary');
    reveal.addEventListener('click', () => {
      shown = !shown;
      secret.textContent = shown ? c.secret_key : '••••••••••••';
      reveal.textContent = shown ? 'Hide' : 'Show';
    });
    const c2 = el('button', 'Copy', 'small secondary');
    c2.addEventListener('click', () => copyText(c.secret_key, 'Secret key'));
    r2.append(el('span', 'Secret key', 'k'), secret, reveal, c2);

    wrap.append(r1, r2);
    box.append(wrap);
  }
  const all = el('button', 'Copy as JSON', 'small secondary');
  all.addEventListener('click', () => copyText(JSON.stringify({credentials: list}, null, 2), 'Credentials (JSON)'));
  const close = el('button', 'Clear', 'small secondary');
  close.addEventListener('click', () => { box.textContent = ''; box.hidden = true; });
  const bar = el('div');
  bar.style.display = 'flex'; bar.style.gap = '6px';
  bar.append(all, close);
  box.append(bar);
  box.hidden = false;
  box.scrollIntoView({block: 'nearest'});
}
