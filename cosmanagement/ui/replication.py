import logging
import time
import types

from dotenv import load_dotenv

from ..cos2pag import teardown as pag_teardown
from .common import LABEL_RE, MAX_RETENTION_DAYS, _api_lock, _text
from .buckets import _existing_bucket


def replication_settings(config):
    """config.json's optional "cos2pag": {"config": "<path to cos2pag's config.yaml>", "env_file": "<path to its .env>"}.
    Returns {config, env_file} or None when replication to PAG is not configured."""
    section = config.get('cos2pag')
    if not isinstance(section, dict) or not isinstance(section.get('config'), str) or not section['config'].strip():
        return None
    env_file = section.get('env_file')
    return {'config': section['config'].strip(), 'env_file': env_file.strip() if isinstance(env_file, str) and env_file.strip() else None}


def _cos2pag():
    """The cos2pag pieces we use (imported lazily: they pull in boto3 and PyYAML)."""
    from ..cos2pag.config import ConfigError, load_config
    from ..cos2pag.http_client import ApiError
    from ..cos2pag.sync import sync_bucket
    return types.SimpleNamespace(ConfigError=ConfigError, ApiError=ApiError, load_config=load_config, sync_bucket=sync_bucket)


class _LogLines(logging.Handler):
    """Collects cos2pag's log records so the page can show them."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(f'{record.levelname:7s} {record.getMessage()}')


class ReplicationInputError(Exception):
    """A request cos2pag should not even be given (shown to the user as is)."""


def _check_partition(cfg, tenant):
    """Refuse a tenant that is not an exact partition name when one exists that differs only by case.

    cos2pag looks the partition up by exact name, finds nothing, tries to create it, and PAG answers that it
    already exists. A tenant with no similar partition at all is fine: cos2pag creates it.
    """
    modules = pag_teardown.load_modules()
    conflict = pag_teardown.partition_conflict(pag_teardown.partition_names(modules, cfg), tenant)
    if conflict:
        raise ReplicationInputError(conflict)


def _cos2pag_call(config, dry_run, work):
    """Run work(cfg, mods) -> (steps, message) with cos2pag's configuration loaded.

    Holds the API lock, loads cos2pag's .env and config.yaml, captures its log lines, and turns every failure
    (not configured, missing file, bad config, API error, anything else) into a message instead of
    a dropped request. Returns {ok, dry_run, message, steps, log}.
    """
    result = {'ok': False, 'dry_run': dry_run, 'message': '', 'steps': [], 'log': ''}
    settings = replication_settings(config)
    if settings is None:
        result['message'] = ('Replication not configured: add "cos2pag": {"config": "<path to cos2pag\'s config.yaml>", '
                             '"env_file": "<path to its .env>"} to config.json.')
        return result
    mods = _cos2pag()

    handler, logger = _LogLines(), logging.getLogger('cosmanagement.cos2pag')
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        with _api_lock:
            try:
                if settings['env_file']:
                    load_dotenv(settings['env_file'], override=True)
                result['steps'], result['message'] = work(mods.load_config(settings['config']), mods)
            except ReplicationInputError as err:
                result['message'] = str(err)
            except FileNotFoundError as err:
                result['message'] = f"File not found: {err.filename or err}"
            except mods.ConfigError as err:
                result['message'] = f'cos2pag configuration: {err}'
            except mods.ApiError as err:
                result['message'] = f'API error: {err}'
            except KeyError as err:
                result['message'] = f'cos2pag configuration: missing key {err}'
            except LookupError as err:
                result['message'] = f'{err}'
            except Exception as err:  # e.g. boto3 / network errors from a step: report them instead of dropping the request
                result['message'] = f'{type(err).__name__}: {err}'
            else:
                result['ok'] = not any(step['status'] == 'FAILED' for step in result['steps'])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    result['log'] = '\n'.join(handler.lines)
    return result


JOB_WAIT_SECONDS = 60       # how long to wait for COS to let PDR's source credentials read the bucket
JOB_WAIT_STEP = 5


def job_wait_seconds(config):
    """config.json "cos2pag": {"job_wait_seconds": N}: how long to wait for COS to honour the new ACL grant before the
    first copy job is started (default 60, 0 to start at once)."""
    value = (config.get('cos2pag') or {}).get('job_wait_seconds')
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3600 else JOB_WAIT_SECONDS


def _is_access_denied(err):
    response = getattr(err, 'response', None)
    code = (response or {}).get('Error', {}).get('Code', '') if isinstance(response, dict) else ''
    status = (response or {}).get('ResponseMetadata', {}).get('HTTPStatusCode') if isinstance(response, dict) else None
    return code in ('AccessDenied', 'Forbidden', '403') or status == 403


def wait_for_source_access(s3, bucket, wait, sleep=time.sleep, step=JOB_WAIT_STEP):
    """List one object of `bucket` with the credentials PDR uses to read it, retrying while COS answers
    AccessDenied for up to `wait` seconds. A grant added to the ACL seconds earlier is not always honoured at once.

    Returns (ready, detail): ready is False only when access is still denied after the wait; any other failure
    (network, TLS...) is not an access problem, so it does not block the job (ready True, with the reason).
    """
    waited = 0
    while True:
        try:
            s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
            return True, f'COS accepted reading the bucket after {waited} s' if waited else 'COS accepted reading the bucket'
        except Exception as err:   # botocore ClientError and anything else
            if not _is_access_denied(err):
                return True, f'access not verified ({type(err).__name__}: {err})'
            if waited >= wait:
                return False, (f"COS still answers AccessDenied when reading the bucket with PDR's source credentials after {waited} s. "
                               "It is not only a matter of delay: check that the access key of pdr.source_s3 belongs to the account "
                               "granted in the ACL (cos.backup_service_account.grantee) and that PDR's IP is in the bucket's whitelist. Once fixed, start a Copy job of the task from PDR.")
        sleep(step)
        waited += step


def _start_initial_job(config, cfg, bucket, steps):
    """Start the first Copy job of a task cos2pag has just created, once COS lets PDR read the bucket.

    cos2pag starts it itself right after patching the ACL, and PDR then failed with "Failed to enumerate objects:
    Forbidden (AccessDenied)" although the grant was in place. Adds a `pdr.job` step to `steps`.
    """
    from ..cos2pag.s3_lifecycle_client import build_s3_client
    mods = pag_teardown.load_modules()
    pdr_cfg = cfg['pdr']
    ready, detail = wait_for_source_access(build_s3_client(pdr_cfg['source_s3']), bucket, job_wait_seconds(config))
    if not ready:
        steps.append({'name': 'pdr.job', 'status': 'FAILED', 'detail': f"initial copy not started: {detail}"})
        return
    client = mods.PdrClient(pdr_cfg['base_url'], pag_teardown._session(mods, pdr_cfg), timeout=pdr_cfg.get('timeout', 30))
    task = client.find_task_by_alias(bucket)
    if task is None or task.get('id') is None:
        steps.append({'name': 'pdr.job', 'status': 'FAILED', 'detail': f"PDR task '{bucket}' not found: initial copy not started"})
        return
    client.start_job(task['id'])
    steps.append({'name': 'pdr.job', 'status': 'CHANGED', 'detail': f"initial copy started (task id={task['id']}); {detail}"})


def run_replication(config, bucket, tenant, dry_run, noncurrent_days=None):
    """Set up (or, with dry_run, preview) the replication of a bucket to PAG with cos2pag's sync_bucket.

    cos2pag does the whole job (COS ACL / firewall / notifications, PAG partition and repository, PDR task) and is
    idempotent; it keeps its own config file and secrets, referenced from config.json. `noncurrent_days` overrides
    the retention of noncurrent object versions (cos2pag's --noncurrent-expiration-days) for this run; applied to
    an existing replication it updates the lifecycle rule on PAG. Returns {ok, dry_run, message, steps: [{name,
    status, detail}], log}.
    """
    def work(cfg, mods):
        _check_partition(cfg, tenant)
        extra = {} if noncurrent_days is None else {'noncurrent_expiration_days': noncurrent_days}
        # cos2pag would start the first copy job at once; we start it ourselves after checking that COS lets PDR read.
        start_job = (cfg.get('pdr') or {}).get('auto_start_job', True) and not dry_run
        if start_job:
            cfg = {**cfg, 'pdr': {**cfg['pdr'], 'auto_start_job': False}}
        report = mods.sync_bucket(cfg, bucket, tenant, dry_run=dry_run, **extra)
        steps = [
            {'name': step.name, 'status': 'SKIPPED' if step.skipped else 'CHANGED' if step.changed else 'OK', 'detail': step.detail}
            for step in report.steps
        ]
        if start_job and any(st['name'] == 'pdr.task' and st['detail'].startswith('created') for st in steps):
            _start_initial_job(config, cfg, bucket, steps)
        message = (f'Simulation finished for {bucket} (no change sent).' if dry_run
                   else f'Replication of {bucket} to the PAG partition {tenant} set up.')
        if any(st['status'] == 'FAILED' for st in steps):
            message = f'Replication of {bucket} set up, but the initial copy did not start (see the detail).'
        return steps, message

    return _cos2pag_call(config, dry_run, work)


TEARDOWN_MESSAGES = {
    'suspend': ('suspended', 'Suspension of the replication of {bucket}'),
    'undo': ('undone (PAG repository kept)', 'Replication of {bucket} undone, PAG repository kept'),
    'purge': ('undone, PAG repository deleted', 'Replication of {bucket} undone, PAG repository deleted'),
}


def run_teardown(config, bucket, tenant, level, dry_run):
    """Suspend or undo the replication of a bucket to PAG (see pag_teardown). Returns the same shape as run_replication."""
    def work(cfg, mods):
        steps = pag_teardown.teardown(pag_teardown.load_modules(), cfg, bucket, tenant, level, dry_run)
        done, label = TEARDOWN_MESSAGES[level]
        failed = [step['name'] for step in steps if step['status'] == 'FAILED']
        if failed:
            message = f"{label.format(bucket=bucket)} interrupted: step {failed[0]} failed."
        elif dry_run:
            message = f'Simulation finished for {bucket} (no change sent).'
        else:
            message = f'Replication of {bucket} {done}.'
        return steps, message

    return _cos2pag_call(config, dry_run, work)


def _cos2pag_clients():
    """The cos2pag client pieces needed to read a replication's state."""
    from ..cos2pag.config import AuthConfig
    from ..cos2pag.http_client import build_session
    from ..cos2pag.pdr_client import PdrClient
    from ..cos2pag.s3_lifecycle_client import build_s3_client, get_bucket_lifecycle
    return types.SimpleNamespace(AuthConfig=AuthConfig, build_session=build_session, PdrClient=PdrClient,
                                 build_s3_client=build_s3_client, get_bucket_lifecycle=get_bucket_lifecycle)


DAY_BITS = [('sunday', 1), ('monday', 2), ('tuesday', 4), ('wednesday', 8), ('thursday', 16), ('friday', 32), ('saturday', 64)]


def noncurrent_days_from_rules(rules):
    """Retention of noncurrent versions (days) from a bucket's S3 lifecycle rules, or None."""
    found = None
    for rule in rules or []:
        days = (rule.get('NoncurrentVersionExpiration') or {}).get('NoncurrentDays')
        if isinstance(days, int) and not isinstance(days, bool):
            if rule.get('ID') == 'expire-noncurrent-versions':
                return days
            found = days if found is None else found
    return found


def replication_status(config, bucket):
    """What exists today for a bucket's replication: the PDR task (alias = bucket name) and the PAG lifecycle rules.

    Read-only: a GET on PDR's task list and a GET of the bucket's lifecycle on PAG's S3 endpoint.
    Returns {ok, message, pdr: {found, id, schedule}, lifecycle: {noncurrent_days, rules}, log}.
    """
    result = {'ok': False, 'message': '', 'pdr': None, 'lifecycle': None, 'log': ''}
    settings = replication_settings(config)
    if settings is None:
        result['message'] = 'Replication not configured.'
        return result
    mods, clients = _cos2pag(), _cos2pag_clients()
    with _api_lock:
        try:
            if settings['env_file']:
                load_dotenv(settings['env_file'], override=True)
            cfg = mods.load_config(settings['config'])
            pdr_cfg = cfg['pdr']
            session = clients.build_session(clients.AuthConfig.from_dict(pdr_cfg.get('auth', {})), verify_ssl=pdr_cfg.get('verify_ssl', True))
            task = clients.PdrClient(pdr_cfg['base_url'], session, timeout=pdr_cfg.get('timeout', 30)).find_task_by_alias(bucket)
            schedule = (task or {}).get('schedule') or {}
            mask = schedule.get('dowMask') if isinstance(schedule.get('dowMask'), int) else 0
            result['pdr'] = {
                'found': task is not None, 'id': (task or {}).get('id'),
                'schedule': {'enabled': bool(schedule.get('enabled')), 'hour': schedule.get('hour'),
                             'days': [name for name, bit in DAY_BITS if mask & bit]} if schedule else None,
            }
            try:
                rules = clients.get_bucket_lifecycle(clients.build_s3_client(pdr_cfg['target_s3']), bucket)
            except Exception:  # no repository on PAG yet (or PAG's S3 not reachable): nothing to read
                rules = None
            result['lifecycle'] = {'noncurrent_days': noncurrent_days_from_rules(rules), 'rules': [r.get('ID') for r in rules or []],
                                   'readable': rules is not None}
            result['ok'] = True
        except FileNotFoundError as err:
            result['message'] = f"File not found: {err.filename or err}"
        except mods.ConfigError as err:
            result['message'] = f'cos2pag configuration: {err}'
        except mods.ApiError as err:
            result['message'] = f'API error: {err}'
        except KeyError as err:
            result['message'] = f'cos2pag configuration: missing key {err}'
        except Exception as err:
            result['message'] = f'{type(err).__name__}: {err}'
    return result


def replication_partitions(config, tenant):
    """The PAG partitions (one per tenant) and how `tenant`, if given, matches them. Read-only."""
    held = {}

    def work(cfg, mods):
        held['names'] = sorted(pag_teardown.partition_names(pag_teardown.load_modules(), cfg), key=str.casefold)
        return [], ''

    result = _cos2pag_call(config, True, work)
    result['names'] = held.get('names', [])
    result['match'] = pag_teardown.resolve_partition(result['names'], tenant) if tenant and result['ok'] else None
    return result


def handle_replication_partitions(config, body):
    tenant = _text(body, 'tenant')
    if tenant and not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only).'}
    return 200, replication_partitions(config, tenant)


def handle_replication_status(config, body):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    return 200, replication_status(config, bucket)


def handle_teardown(config, body, dry_run):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    level = _text(body, 'level')
    if level not in pag_teardown.LEVELS:
        return 400, {'error': 'Invalid level (suspend, undo or purge).'}
    tenant = _text(body, 'tenant')
    if (level == 'purge' or tenant) and not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only); it is required to delete the repository.'}
    if not dry_run and level != 'suspend' and _text(body, 'confirm') != bucket:
        return 400, {'error': 'Wrong confirmation: type the exact bucket name.'}
    return 200, run_teardown(config, bucket, tenant, level, dry_run)


def handle_replication(config, body, dry_run):
    bucket = _existing_bucket(body)
    if not bucket:
        return 400, {'error': 'Invalid bucket name.'}
    tenant = _text(body, 'tenant')
    if not LABEL_RE.match(tenant):
        return 400, {'error': 'Invalid PAG tenant (letters, digits, . - and _ only).'}
    days = None
    if _text(body, 'noncurrent_days'):
        try:
            days = int(_text(body, 'noncurrent_days'))
        except ValueError:
            days = 0
        if not 1 <= days <= MAX_RETENTION_DAYS:
            return 400, {'error': f'Invalid retention (whole number of days, between 1 and {MAX_RETENTION_DAYS}).'}
    return 200, run_replication(config, bucket, tenant, dry_run, days)
