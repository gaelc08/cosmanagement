"""
Suspend or undo the replication of a bucket to PAG that cos2pag set up.

cos2pag only creates (and updates). Undoing it means reversing what it did, in the reverse order of its
pipeline COS -> PAG -> PDR:

  PDR   the replication task, whose alias is the bucket name
  PAG   the object repository named like the bucket, in the tenant's partition (and its lifecycle rules)
  COS   the ACL grant for the backup service account, the PDR IP in the firewall, the notifications topic

Three levels:

  suspend   disable the schedule and the notifications of the PDR task. Nothing is deleted; running cos2pag
            again on the bucket (the "Exécuter" of the replication panel) re-enables them.
  undo      delete the PDR task and take back from COS what cos2pag added (the ACL grant, the PDR IP).
            The PAG repository, its data and its lifecycle rules are kept.
  purge     undo, and also delete the PAG repository: the archived copies are lost.

The partition (shared by the whole tenant) and its persistent buffer are never touched. The notifications topic
is left as it is: its value before cos2pag is unknown. The firewall whitelist is never emptied.

The calls cos2pag does not make are assumptions to check against the products' API guides:
  DELETE /api/tasks/{id}                                          (PDR; given by the user)
  PATCH  /api/tasks/{id} with {"schedule"|"notifications": {"enabled": false}}   (PDR)
  DELETE /api/partitions/{partitionUuid}/repositories/{repositoryUuid}           (PAG)
Their status and body are returned when they fail.

Uses cos2pag's own clients (an optional dependency), so it shares its configuration and secrets.
"""

import types

LEVELS = ('suspend', 'undo', 'purge')


def load_modules():
    """The cos2pag pieces used here, or None when the package is not installed."""
    try:
        from cos2pag.config import AuthConfig
        from cos2pag.cos_client import CosClient, acl_map_to_pairs
        from cos2pag.http_client import build_session, request_json
        from cos2pag.pag_client import PagClient
        from cos2pag.pdr_client import PdrClient
    except ImportError:
        return None
    return types.SimpleNamespace(
        AuthConfig=AuthConfig, CosClient=CosClient, acl_map_to_pairs=acl_map_to_pairs, build_session=build_session,
        request_json=request_json, PagClient=PagClient, PdrClient=PdrClient,
    )


class PartitionNameError(Exception):
    """The tenant does not name a PAG partition exactly, but one that differs only by case."""


def partition_names(mods, cfg):
    """Names of the PAG partitions (one per tenant), read with cos2pag's own client."""
    pag_cfg = cfg['pag']
    client = mods.PagClient(pag_cfg['base_url'], _session(mods, pag_cfg), timeout=pag_cfg.get('timeout', 30))
    return [partition['name'] for partition in client.list_partitions() if partition.get('name')]


def resolve_partition(names, tenant):
    """How a tenant name matches the existing partitions: {match: exact | case | ambiguous | none, name | candidates}.

    cos2pag compares partition names exactly, case included, while PAG refuses to create a partition whose name
    differs from an existing one only by case ("the partition does already exist").
    """
    if tenant in names:
        return {'match': 'exact', 'name': tenant}
    folded = [name for name in names if name.casefold() == tenant.casefold()]
    if len(folded) == 1:
        return {'match': 'case', 'name': folded[0]}
    if folded:
        return {'match': 'ambiguous', 'candidates': sorted(folded)}
    return {'match': 'none'}


def partition_conflict(names, tenant):
    """The error to show when `tenant` is not an exact partition name but an existing one differs only by case, else None."""
    found = resolve_partition(names, tenant)
    if found['match'] == 'case':
        return (f"Une partition PAG existe sous le nom « {found['name']} » (casse différente de « {tenant} »). cos2pag compare les noms "
                f"exactement : il tenterait de la recréer et PAG refuserait (« already exist »). Utilise exactement « {found['name']} ».")
    if found['match'] == 'ambiguous':
        return (f"Plusieurs partitions PAG ne diffèrent de « {tenant} » que par la casse : {', '.join(found['candidates'])}. "
                'Choisis le nom exact.')
    return None


def _session(mods, section):
    auth = mods.AuthConfig.from_dict(section.get('auth', {}))
    return mods.build_session(auth, verify_ssl=section.get('verify_ssl', True))


def teardown(mods, cfg, bucket, tenant, level, dry_run):
    """Run (or, with dry_run, only describe) the chosen level. Returns the list of steps
    [{name, status, detail}], status being CHANGED, SKIPPED, FAILED or NOT_RUN.

    Steps run in order and stop at the first failure; the ones after it are reported as NOT_RUN. Every step
    first reads the current state, so running it again after a partial failure only does what is left.
    """
    if level not in LEVELS:
        raise ValueError(f'unknown level {level!r}')
    pdr_cfg, cos_cfg, pag_cfg = cfg['pdr'], cfg['cos'], cfg['pag']
    suffix = ' [dry-run, no request sent]' if dry_run else ''
    cache = {}

    def pdr_client():
        return mods.PdrClient(pdr_cfg['base_url'], _session(mods, pdr_cfg), timeout=pdr_cfg.get('timeout', 30), dry_run=dry_run)

    def find_task():
        if 'task' not in cache:
            cache['task'] = pdr_client().find_task_by_alias(bucket)
        return cache['task']

    def pdr_suspend():
        task = find_task()
        if task is None:
            return 'SKIPPED', f"aucune tâche PDR pour le bucket '{bucket}'"
        body = {}
        if (task.get('schedule') or {}).get('enabled'):
            body['schedule'] = {'enabled': False}
        if (task.get('notifications') or {}).get('enabled'):
            body['notifications'] = {'enabled': False}
        if not body:
            return 'SKIPPED', f"tâche id={task['id']} déjà suspendue (planification et notifications désactivées)"
        pdr_client().update_task(task['id'], body)
        return 'CHANGED', f"tâche id={task['id']} suspendue : {', '.join(sorted(body))} désactivé(es){suffix}"

    def pdr_delete():
        task = find_task()
        if task is None:
            return 'SKIPPED', f"aucune tâche PDR pour le bucket '{bucket}'"
        client = pdr_client()
        mods.request_json(client.session, 'DELETE', f"{client.base_url}/api/tasks/{task['id']}", timeout=client.timeout, dry_run=dry_run)
        return 'CHANGED', f"tâche id={task['id']} supprimée{suffix}"

    def pag_keep():
        return 'SKIPPED', f"dépôt PAG '{bucket}' conservé, avec ses données et ses règles de cycle de vie"

    def pag_delete():
        client = mods.PagClient(pag_cfg['base_url'], _session(mods, pag_cfg), timeout=pag_cfg.get('timeout', 30), dry_run=dry_run)
        conflict = partition_conflict([p['name'] for p in client.list_partitions() if p.get('name')], tenant)
        if conflict:
            raise PartitionNameError(conflict)
        partition = client.find_partition_optional(tenant)
        if partition is None:
            return 'SKIPPED', f"partition PAG '{tenant}' introuvable : rien à supprimer"
        repo = client.find_repository(partition['uuid'], bucket)
        if repo is None:
            return 'SKIPPED', f"pas de dépôt '{bucket}' dans la partition '{tenant}'"
        mods.request_json(client.session, 'DELETE', f"{client.base_url}/api/partitions/{partition['uuid']}/repositories/{repo['uuid']}",
                          timeout=client.timeout, dry_run=dry_run)
        return 'CHANGED', f"dépôt '{bucket}' supprimé de la partition '{tenant}' (copies archivées perdues; la partition est conservée){suffix}"

    def cos_client():
        return mods.CosClient(cos_cfg['base_url'], _session(mods, cos_cfg), timeout=cos_cfg.get('timeout', 30), dry_run=dry_run)

    def cos_bucket():
        if 'bucket' not in cache:
            client = cos_client()
            cache['cos'] = client
            cache['bucket'] = client.get_bucket(bucket)
        return cache['cos'], cache['bucket']

    def cos_revert():
        client, info = cos_bucket()
        account = cos_cfg['backup_service_account']
        grantee, permission = account['grantee'], account.get('permission', 'READ')
        pdr_ip = cos_cfg['pdr_ip']

        pairs = mods.acl_map_to_pairs(info.get('acl'))
        kept = [p for p in pairs if not (p['grantee'] == grantee and p['permission'] == permission)]
        acl_changed = len(kept) != len(pairs)

        allowed = (info.get('firewall') or {}).get('allowed_ip')
        ip_changed, ip_note = False, ''
        if allowed is not None and pdr_ip in allowed:
            remaining = [ip for ip in allowed if ip != pdr_ip]
            if remaining:
                ip_changed = True
            else:
                ip_note = f" ; {pdr_ip} est la seule IP de la liste blanche : laissée, car une liste vide pourrait ouvrir le bucket à toutes les IP"

        body = {}
        if acl_changed:
            body['acl'] = kept
        if ip_changed:
            body['firewall'] = {'allowed_ip': [ip for ip in allowed if ip != pdr_ip]}
        if not body:
            return 'SKIPPED', f'rien à retirer du bucket (ACL de {grantee}, IP {pdr_ip}){ip_note}'
        client.patch_bucket(bucket, body, if_unmodified_since=info.get('time_updated'))
        return 'CHANGED', f'acl_retiree={acl_changed} ip_retiree={ip_changed}{ip_note}{suffix}'

    def cos_notifications():
        _, info = cos_bucket()
        topic = (info.get('notifications') or {}).get('topic')
        if topic == bucket:
            return 'SKIPPED', f"topic '{topic}' laissé tel quel : sa valeur avant cos2pag n'est pas connue"
        return 'SKIPPED', 'topic de notification non posé par cos2pag : rien à faire'

    plans = {
        'suspend': [('pdr.task', pdr_suspend)],
        'undo': [('pdr.task', pdr_delete), ('pag.repository', pag_keep), ('cos.acl_and_firewall', cos_revert), ('cos.notifications', cos_notifications)],
        'purge': [('pdr.task', pdr_delete), ('pag.repository', pag_delete), ('cos.acl_and_firewall', cos_revert), ('cos.notifications', cos_notifications)],
    }

    steps, failed = [], False
    for name, action in plans[level]:
        if failed:
            steps.append({'name': name, 'status': 'NOT_RUN', 'detail': 'non exécuté : une étape précédente a échoué'})
            continue
        try:
            status, detail = action()
        except KeyError:
            raise  # a missing key in cos2pag's config: the caller reports it as a configuration error
        except PartitionNameError as err:
            steps.append({'name': name, 'status': 'FAILED', 'detail': str(err)})
            failed = True
        except Exception as err:  # keep the steps already done in the report instead of losing them
            steps.append({'name': name, 'status': 'FAILED', 'detail': f'{type(err).__name__} : {err}'})
            failed = True
        else:
            steps.append({'name': name, 'status': status, 'detail': detail})
    return steps
