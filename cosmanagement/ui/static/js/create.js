// The bucket form offers the storage locations of the account's tenant as buttons. A single one is filled in;
// with several (internal / external endpoint) you choose, so nothing is picked silently.
function syncCreateLocation() {
  const account = $('c-account').value.trim();
  const field = $('c-location');
  const choices = ACCOUNT_OK.test(account) ? setLocationSuggestions(account) : [];
  const box = $('c-location-choices');
  box.textContent = '';
  for (const name of choices) {
    const b = el('button', labelFor(name), 'small secondary');
    b.type = 'button';
    b.addEventListener('click', () => { field.value = name; delete field.dataset.auto; });
    box.append(b);
  }
  if (choices.length === 1 && (!field.value.trim() || field.dataset.auto)) { field.value = choices[0]; field.dataset.auto = '1'; }
  else if (field.dataset.auto && !choices.includes(field.value)) { field.value = ''; delete field.dataset.auto; }
}
const ACCOUNT_OK = /^sa-[A-Za-z0-9][A-Za-z0-9_-]*$/;
$('c-account').addEventListener('input', syncCreateLocation);
$('c-location').addEventListener('input', () => { delete $('c-location').dataset.auto; });


// ---- create one account / one bucket ---------------------------------------
async function afterCreate(account) {
  if (lastSearch && lastSearch.kind === 'tenant' && account.startsWith('sa-' + lastSearch.value + '-')) await refreshSearch();
}

$('account-form').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {account: $('a-id').value.trim(), account_meta_name: $('a-meta').value.trim()};
  if (!confirm('Create the storage account "' + body.account + '"\n  x-Account-Meta-name: ' + (body.account_meta_name || '(none)'))) return;
  const btn = $('a-go');
  btn.disabled = true;
  setStatus('Creating the account...', false);
  const out = await post('/api/account/create', body);
  btn.disabled = false;
  if (out.log) showLog(out.log);
  setStatus(out.error || out.message, !!out.error || !out.ok);
  if (out.ok) await afterCreate(body.account);
});

$('bucket-form').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {
    account: $('c-account').value.trim(),
    bucket: $('c-bucket').value.trim(),
    quota_gb: $('c-quota').value.trim(),
    storage_location: $('c-location').value.trim(),
    allowed_ips: $('c-ips').value,
    account_meta_name: $('c-meta').value.trim(),
  };
  const ips = body.allowed_ips.split(/[\s,;]+/).filter(Boolean).length;
  const summary = 'Create the bucket "' + body.bucket + '"\n' +
    '  account: ' + body.account + '\n' +
    '  quota: ' + body.quota_gb + ' GB\n' +
    '  storage location: ' + body.storage_location + '\n' +
    '  allowed IPs: ' + ips + '\n' +
    '  x-Account-Meta-name: ' + (body.account_meta_name || '(none)');
  if (!confirm(summary)) return;
  const btn = $('c-go');
  btn.disabled = true;
  setStatus('Creating the bucket...', false);
  const out = await post('/api/bucket/create', body);
  btn.disabled = false;
  if (out.log) showLog(out.log);
  setStatus(out.error || out.message, !!out.error || !out.ok);
  if (out.ok) {
    $('c-bucket').value = '';
    await afterCreate(body.account);
  }
});
$('a-id').addEventListener('input', () => { delete $('a-id').dataset.prefilled; });
