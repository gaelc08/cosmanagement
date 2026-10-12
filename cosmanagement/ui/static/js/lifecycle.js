// ---- lifecycle: expire the objects of the bucket's source (COS) after X days ---------------------------------
async function openLifecycle(account, bucket) {
  const box = panelHeader('Lifecycle: expire objects', $('m-life'));
  box.append(el('div', 'Reading the rules...', 'empty'));
  const out = await post('/api/lifecycle/get', {account, bucket});
  box.textContent = '';
  box.append(el('strong', 'Lifecycle: expire objects'));
  if (out.log) showLog(out.log);
  if (out.error || !out.ok) {
    box.append(el('div', 'Rules unavailable: ' + (out.error || out.message), 'empty'));
    return;
  }
  box.append(el('div', 'Deletes objects of this bucket on COS, with no way back. With versioning on, expiring the current ' +
    'version only adds a delete marker; the noncurrent field removes the older versions for good. Rules that are not ' +
    'managed here are kept as they are.', 'empty'));

  const warning = el('div');
  box.append(warning);
  if (replicationConfigured) {
    post('/api/replication/status', {bucket}).then(st => {
      if (!st.ok || !st.pdr || !st.pdr.found) return;
      warning.className = st.pdr.deletion_enabled ? 'err-list' : 'empty';
      warning.textContent = st.pdr.deletion_enabled
        ? 'This bucket is replicated and its PDR task mirrors deletions: objects expired here will also be deleted from the PAG copy.'
        : 'This bucket is replicated; its PDR task does not mirror deletions, so PAG keeps its copies.';
    });
  }

  if (out.others.length) {
    box.append(el('div', 'Other rules on the bucket (not editable here)', 'empty'));
    box.append(table(['Rule', 'Status', 'What it does'], out.others.map(r => [r.id, r.status, r.summary])));
  }

  const form = el('div', undefined, 'grid-form');
  const field = (label, value, placeholder, numeric) => {
    const input = el('input'); input.type = 'text'; input.placeholder = placeholder;
    if (numeric) input.inputMode = 'numeric';
    input.value = value === null || value === undefined ? '' : String(value);
    form.append(el('label', label), input);
    return input;
  };
  const expire = field('Expire objects after (days)', out.values.expire_days, 'empty: no expiration', true);
  const noncurrent = field('Expire noncurrent versions after (days)', out.values.noncurrent_days, 'empty: keep them', true);
  const abort = field('Abort incomplete uploads after (days)', out.values.abort_days, 'empty: keep them', true);
  const prefix = field('Only objects under the prefix', out.values.prefix, 'empty: the whole bucket', false);
  const buttons = el('div', undefined, 'buttons');
  const preview = el('button', 'Preview'); preview.type = 'button';
  const save = el('button', 'Save', 'danger'); save.type = 'button'; save.disabled = true;
  buttons.append(preview, save);
  form.append(buttons);
  const result = el('div');
  box.append(form, result);

  const request = () => ({account, bucket, expire_days: expire.value, noncurrent_days: noncurrent.value, abort_days: abort.value, prefix: prefix.value});
  let previewed = null;
  const lock = () => { previewed = null; save.disabled = true; };
  [expire, noncurrent, abort, prefix].forEach(f => f.addEventListener('input', lock));

  async function call(dry) {
    const body = request();
    if (!dry) {
      const typed = prompt('Change the lifecycle rules of "' + bucket + '" (' + account + '). Objects that match will be deleted for good.\n' +
                           'Type the bucket name to confirm.');
      if (typed !== bucket) { setStatus('Operation cancelled.', false); return; }
      body.confirm = typed;
    }
    preview.disabled = true; save.disabled = true;
    setStatus(dry ? 'Computing the rules...' : 'Saving the rules...', false);
    result.textContent = '';
    const res = await post('/api/lifecycle/' + (dry ? 'preview' : 'save'), body);
    preview.disabled = false;
    if (res.log) showLog(res.log);
    const done = !res.error && res.ok;
    setStatus(res.error || res.message, !done);
    if (done) {
      result.append(el('strong', dry ? 'Rules after saving' : 'Rules now on the bucket'));
      result.append(res.resulting.length
        ? table(['Rule', 'Status', 'What it does'], res.resulting.map(r => [r.id, r.status, r.summary]))
        : el('div', 'No rule: the bucket has no lifecycle configuration.', 'empty'));
    }
    if (dry && done && res.changed) { previewed = JSON.stringify(request()); save.disabled = false; }
    else if (!dry && done) { await openLifecycle(account, bucket); setStatus(res.message, false); }
  }
  preview.addEventListener('click', () => call(true));
  save.addEventListener('click', () => { if (JSON.stringify(request()) === previewed) call(false); else lock(); });
}
