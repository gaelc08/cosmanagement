// ---- replication to PAG (cos2pag) -------------------------------------------------
const STEP_STATE = {
  preview: {CHANGED: 'To do', SKIPPED: 'Already in place', OK: 'OK'},
  run: {CHANGED: 'Done', SKIPPED: 'Already in place', OK: 'OK'},
};

const TEARDOWN_STATE = {
  preview: {CHANGED: 'To do', SKIPPED: 'Nothing to do', FAILED: 'Failed', NOT_RUN: 'Not run'},
  run: {CHANGED: 'Done', SKIPPED: 'Nothing to do', FAILED: 'Failed', NOT_RUN: 'Not run'},
};
const DAY_NAMES = {sunday: 'Sunday', monday: 'Monday', tuesday: 'Tuesday', wednesday: 'Wednesday', thursday: 'Thursday', friday: 'Friday', saturday: 'Saturday'};

function openReplication(account, bucket) {
  const box = panelHeader('Replicate to PAG: ' + bucket + ' (' + account + ')', $('m-repl'));
  const close = el('button', 'Close', 'small secondary');
  close.type = 'button';
  close.addEventListener('click', () => closePanel(box));
  box.scrollIntoView({block: 'nearest'});
  if (!replicationConfigured) {
    box.append(el('p', 'Replication to PAG is not configured. Install cos2pag in the same Python environment (pip install <cos2pag folder>) ' +
      'and add to config.json: "cos2pag": {"config": "<path to cos2pag\'s config.yaml>", "env_file": "<path to its .env>"}.', 'empty'), close);
    return;
  }
  box.append(el('p', 'Chains what cos2pag does: COS (ACL, IP whitelist and notifications of the bucket), PAG (partition, repository, buffer, lifecycle), then ' +
    'PDR (replication task). Every step is idempotent. To edit an existing replication, change the retention then simulate: ' +
    'the steps that would change show as "To do". "Simulate" sends no change; "Run" is only possible after a successful simulation.', 'empty'));

  const statusBox = el('div');
  box.append(statusBox);
  const form = el('div', undefined, 'grid-form');
  const tenant = el('input');
  tenant.type = 'text'; tenant.maxLength = 64; tenant.value = tenantOf(account).toUpperCase();
  tenant.setAttribute('list', 'partitions-list');
  const tenantHint = el('small', '', 'tenant-hint');
  const retention = el('input');
  retention.type = 'text'; retention.inputMode = 'numeric'; retention.placeholder = 'cos2pag value (pag.lifecycle)';
  form.append(el('label', 'PAG tenant (partition)'), tenant,
              el('small', 'Exact name of the PAG partition (one per tenant, case matters). Check it: several tenants share a prefix (ME, ME-SR, ME-SRE...).'),
              tenantHint,
              el('label', 'Retention (days)'), retention,
              el('small', 'How long noncurrent versions are kept on PAG (--noncurrent-expiration-days). Empty: keep the value from cos2pag\'s config.yaml.'));
  const buttons = el('div', undefined, 'buttons');
  const simulate = el('button', 'Simulate (dry-run)'); simulate.type = 'button';
  const run = el('button', 'Run', 'secondary'); run.type = 'button'; run.disabled = true;
  buttons.append(simulate, run, close);
  form.append(buttons);
  const result = el('div');
  box.append(form, result);

  const key = () => tenant.value.trim() + '|' + retention.value.trim();
  let simulatedFor = null;
  const lock = () => { simulatedFor = null; run.disabled = true; };
  tenant.addEventListener('input', lock);
  retention.addEventListener('input', lock);

  async function refreshStatus(prefill) {
    statusBox.textContent = '';
    const out = await post('/api/replication/status', {bucket});
    if (out.error || !out.ok) {
      statusBox.append(el('div', 'Replication state unreadable: ' + (out.error || out.message), 'empty'));
      return;
    }
    const rows = [['Replication task (PDR)', out.pdr.found ? 'present (id ' + out.pdr.id + ')' : 'absent: not replicated yet']];
    const sch = out.pdr.schedule;
    if (out.pdr.found && sch) {
      rows.push(['Schedule', (sch.enabled ? 'enabled' : 'disabled') + (sch.days.length ? ' : ' + sch.days.map(d => DAY_NAMES[d]).join(', ') : '') +
                                  (sch.hour !== null && sch.hour !== undefined ? ' at ' + sch.hour + ':00' : '')]);
    }
    const days = out.lifecycle.noncurrent_days;
    rows.push(['Noncurrent version retention (PAG)', days !== null ? days + ' days' : (out.lifecycle.readable ? 'not set' : 'unreadable (no repository on PAG yet?)')]);
    statusBox.append(el('strong', 'Current state'), table(['Item', 'Value'], rows));
    if (prefill && days !== null && !retention.value.trim()) retention.value = String(days);
  }
  refreshStatus(true);

  // ---- suspend or undo --------------------------------------------------------------
  const down = el('fieldset');
  down.append(el('legend', 'Suspend or undo the replication'));
  for (const [value, title, text] of [
    ['suspend', 'Suspend the task in PDR', 'Disables the task\'s schedule and notifications, deleting nothing. Reversible: "Run" above resumes the replication.'],
    ['undo', 'Undo everything, keep the PAG repository', 'Deletes the PDR task and removes from COS the ACL and the PDR IP added for the replication. The PAG repository, its data and its lifecycle are kept.'],
    ['purge', 'Undo everything, including the PAG repository', 'As above, and deletes the PAG repository: the archived copies are lost. Irreversible.'],
  ]) {
    const label = el('label');
    label.style.display = 'block'; label.style.marginBottom = '6px';
    const radio = el('input');
    radio.type = 'radio'; radio.name = 'down-level'; radio.value = value; radio.checked = value === 'suspend';
    label.append(radio, el('strong', ' ' + title), el('div', text, 'empty'));
    down.append(label);
  }
  const downSim = el('button', 'Simulate'); downSim.type = 'button';
  const downRun = el('button', 'Run', 'danger'); downRun.type = 'button'; downRun.disabled = true;
  const downButtons = el('div', undefined, 'row-buttons');
  downButtons.append(downSim, downRun);
  const downResult = el('div');
  down.append(downButtons, downResult);
  box.append(down);

  const level = () => down.querySelector('input[name=down-level]:checked').value;
  const downKey = () => level() + '|' + tenant.value.trim();
  let downSimulated = null;
  const downLock = () => { downSimulated = null; downRun.disabled = true; };
  down.querySelectorAll('input[name=down-level]').forEach(r => r.addEventListener('change', downLock));
  tenant.addEventListener('input', downLock);

  async function callDown(dry) {
    const lv = level(), name = tenant.value.trim();
    if (lv === 'purge' && !name) { setStatus('Enter the PAG tenant to delete the repository.', true); return; }
    let typed;
    if (!dry) {
      if (lv === 'suspend') {
        if (!confirm('Suspend the replication of "' + bucket + '"? The PDR task is not deleted.')) return;
      } else {
        const warning = lv === 'purge' ? 'WARNING: the PAG repository will be deleted along with the archived copies. This is irreversible.\n\n' : '';
        typed = prompt(warning + 'Undo the replication of "' + bucket + '": delete the PDR task and remove the access added on COS.\nType the bucket name to confirm.');
        if (typed !== bucket) { setStatus('Operation cancelled.', false); return; }
      }
    }
    downSim.disabled = true; downRun.disabled = true;
    setStatus(dry ? 'Simulation in progress...' : 'Running...', false);
    downResult.textContent = '';
    const out = await post(dry ? '/api/replication/teardown/preview' : '/api/replication/teardown/run', {bucket, tenant: name, level: lv, confirm: typed});
    downSim.disabled = false;
    showLog(out.log);
    setStatus(out.error || out.message, !!out.error || !out.ok);
    if (out.steps && out.steps.length) {
      const states = TEARDOWN_STATE[dry ? 'preview' : 'run'];
      downResult.append(table(['Step', 'State', 'Detail'], out.steps.map(st => [st.name,
        {text: states[st.status] || st.status, cls: st.status === 'CHANGED' ? 'ok' : st.status === 'FAILED' ? 'fail' : undefined}, st.detail])));
    }
    if (dry && out.ok) { downSimulated = downKey(); downRun.disabled = false; }
    else { downSimulated = null; downRun.disabled = true; }
    if (!dry && out.steps && out.steps.length) refreshStatus(false);
  }
  downSim.addEventListener('click', () => callDown(true));
  downRun.addEventListener('click', () => callDown(false));

  async function call(dry) {
    const name = tenant.value.trim();
    if (!name) { setStatus('Enter the PAG tenant.', true); return; }
    const days = retention.value.trim();
    if (!dry && !confirm('Set up the replication of "' + bucket + '" to the PAG partition "' + name + '"?' +
        (days ? '\nNoncurrent version retention: ' + days + ' days.' : '') + '\n\n' +
        'This changes the bucket on the COS side (ACL, IP whitelist, notifications) and creates or updates objects in PAG and PDR.')) return;
    simulate.disabled = true; run.disabled = true;
    setStatus(dry ? 'Simulation in progress...' : 'Setting up the replication...', false);
    result.textContent = '';
    const out = await post(dry ? '/api/replication/preview' : '/api/replication/run', {bucket, tenant: name, noncurrent_days: days});
    simulate.disabled = false;
    showLog(out.log);
    setStatus(out.error || out.message, !!out.error || !out.ok);
    if (out.steps && out.steps.length) {
      const states = STEP_STATE[dry ? 'preview' : 'run'];
      result.append(table(['Step', 'State', 'Detail'], out.steps.map(st => [st.name, {text: states[st.status] || (st.status === 'FAILED' ? 'Failed' : st.status), cls: st.status === 'CHANGED' ? 'ok' : st.status === 'FAILED' ? 'fail' : undefined}, st.detail])));
    }
    if (dry && out.ok) { simulatedFor = key(); run.disabled = false; }
    else if (!dry && out.ok) { simulatedFor = null; refreshStatus(false); }
    else { run.disabled = simulatedFor !== key(); }
  }
  simulate.addEventListener('click', () => call(true));
  run.addEventListener('click', () => call(false));

  // ---- the tenant must name an existing partition exactly, or none at all -----------------
  // cos2pag looks the partition up by exact name; PAG refuses to create one whose name differs only by case.
  const partitionList = el('datalist');
  partitionList.id = 'partitions-list';
  box.append(partitionList);
  let partitionNames = null, tenantTouched = false;

  function describeTenant() {
    tenantHint.textContent = '';
    tenantHint.className = 'tenant-hint';
    const name = tenant.value.trim();
    if (partitionNames === null || !name) return;
    const same = partitionNames.filter(n => n.toLowerCase() === name.toLowerCase());
    if (partitionNames.includes(name)) {
      tenantHint.textContent = 'The PAG partition "' + name + '" exists: it will be used, nothing is created.';
      tenantHint.classList.add('ok');
    } else if (same.length) {
      tenantHint.classList.add('bad');
      tenantHint.append(same.length === 1
        ? 'A partition exists under the name "' + same[0] + '" (different case): cos2pag would recreate it and PAG would refuse. '
        : 'Several partitions differ only by case: ' + same.join(', ') + '. ');
      for (const n of same) {
        const use = el('button', 'Use "' + n + '"', 'small secondary');
        use.type = 'button';
        use.addEventListener('click', () => { tenant.value = n; tenantTouched = true; lock(); downLock(); describeTenant(); });
        tenantHint.append(use);
      }
    } else {
      tenantHint.textContent = 'No partition "' + name + '": cos2pag will create it with the new_partition_defaults of its config.yaml.';
    }
  }
  tenant.addEventListener('input', () => { tenantTouched = true; describeTenant(); });

  (async () => {
    const out = await post('/api/replication/partitions', {});
    if (out.error || !out.ok) {
      tenantHint.textContent = 'PAG partitions unreadable: ' + (out.error || out.message);
      tenantHint.classList.add('bad');
      return;
    }
    partitionNames = out.names;
    for (const n of partitionNames) { const o = el('option'); o.value = n; partitionList.append(o); }
    if (!tenantTouched) {   // the upper-cased guess is only a guess: prefer the partition that really exists
      const guess = tenantOf(account).toLowerCase();
      const best = partitionNames.includes(tenant.value) ? tenant.value : partitionNames.find(n => n.toLowerCase() === guess);
      if (best && best !== tenant.value) { tenant.value = best; lock(); downLock(); }
    }
    describeTenant();
  })();
}

async function deleteBucket(account, bucket) {
  const typed = prompt('Permanently delete the bucket "' + bucket + '" (account ' + account + ').\n' +
                       'This cannot be undone. Type the bucket name to confirm.');
  if (typed !== bucket) { setStatus('Deletion cancelled.', false); return; }
  setStatus('Deleting...', false);
  const out = await post('/api/bucket/delete', {bucket, confirm: typed});
  if (out.log) showLog(out.log);
  const ok = !out.error && out.ok;
  setStatus(out.error || out.message, !ok);
  if (ok) {
    for (const list of Object.values(data)) { const i = list.indexOf(bucket); if (i >= 0) list.splice(i, 1); }
    if (info[account]) delete info[account][bucket];
    if (lastSearch && lastSearch.kind === 'bucket') for (const acct of Object.keys(data)) if (!data[acct].length) delete data[acct];
    selected = null;
    renderSelected();
    showTab('search');
    render();
  }
}
