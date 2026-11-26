"""Weapon, attachment and head tables mined from property objects.

``util::Token`` values are CRC32 hashes of their text: ``crc32('scope_ironsights_rear')`` is the
token 767F2D49 that the M4's ``required_sight_attachment`` holds. (Model and texture names are
hashed from something else, probably their source paths, so they still have no readable form.)

Where the tables live (all in ``MAIN.PSARC``):

* ``equipment.zrb``: ``WeaponData`` (``id``, third-person ``model_name``,
  ``required_sight_attachment``, ``attachment_list`` of ``WeaponAttachment.name`` tokens = crc32
  of attachment ids) and ``AttachmentData`` (``id``, ``attachment_type``, third-person
  ``model_name``).
* ``streamed_weapons.zrb`` / ``streamed_attachments.zrb``: ``StreamedItemData`` (``id`` ->
  first-person ``model_name``). The first-person weapons have no stock (it is never in view).
* ``vehicles.zrb``: ``VehicleData`` (``ModelFileName``, ``PropertiesFileName`` = the vehicle's
  properties record, type 6DAF4680, in the vehicle's own ZRB). That record holds ``TurretProperties``
  (``Weapon1``/``Weapon2`` = tokens of ``WeaponData`` ids, hashed lowercase: ``m2hb_vehicle``) and
  ``SeatProperties`` (``Name`` = the seat's turret node, ``TurretIndex`` = its turret).
* ``character_class.zrb``: ``CharacterHeadDataTable`` (``head_model_name`` = the head record,
  one ``CharacterHeadData`` per head: ``head_model_index`` and a ``skin_tone`` enum).

Attachments sit on named nodes of the weapon model; the node names are crc32 hashes too.
"""
import zlib

from . import props

# attachment_type enum -> socket node of the weapon model
SOCKET_FOR_TYPE = {1: 'underbarrel', 2: 'sight', 3: 'firepoint', 5: 'underbarrel', 6: 'underbarrel'}
ATTACHMENT_TYPES = {1: 'launcher', 2: 'sight', 3: 'muzzle', 5: 'bipod', 6: 'foregrip'}

# model node names recovered by hashing candidate words (crc32)
NODE_NAMES = {zlib.crc32(s.encode()): s for s in (
    'firepoint', 'sight', 'underbarrel', 'eject', 'mag', 'trigger', 'handik',
    'turret_01', 'turret_yaw', 'turret_pitch', 'gunner_seat', 'driver_seat')}


def token(s):
    return zlib.crc32(s.encode('latin1'))


def _enum(obj, name):
    f = obj.fields.get(name)
    if f and f[0] == 'enum' and f[1]:
        return f[1][-1]
    return None


def _u8(obj, name):
    f = obj.fields.get(name)
    if f and f[1]:
        return f[1][0]
    return None


def empty():
    return dict(weapons={}, attachments={}, fp_weapons={}, fp_attachments={}, heads=None,
                vehicles={}, turrets={})


def _turret_tables(data, spans, out):
    for name, start, end in spans or ():
        sub = data[start:end]
        if b'TurretProperties\0' not in sub:
            continue
        weapons = [[o.token('Weapon1') or 0, o.token('Weapon2') or 0]
                   for o in props.iter_objects(sub, type_name='TurretProperties')]
        seats = []
        for o in props.iter_objects(sub, type_name='SeatProperties'):
            f = o.fields.get('TurretIndex')
            if f and len(f[1]) >= 4:
                seats.append((o.token('Name') or 0, int.from_bytes(f[1][:4], 'big', signed=True)))
        out['turrets'].setdefault(name, dict(weapons=weapons, seats=seats))


def scan(data, stem, out, spans=None):
    """Add the tables found in one ZRB's property objects to ``out`` (see ``empty``).

    weapons:        id -> dict(model, sight, attachments=[attachment tokens])
    attachments:    token -> dict(id, type, model)
    fp_weapons:     id -> first-person model hash      (streamed_weapons)
    fp_attachments: id -> first-person model hash      (streamed_attachments)
    heads:          dict(model, tones=[skin tone per head index])
    vehicles:       vehicle model hash -> properties record name hash
    turrets:        properties record name hash -> dict(weapons=[[Weapon1, Weapon2] tokens per
                    turret], seats=[(seat node hash, turret index)])

    ``spans``: (record name hash, payload start, end) of the ZRB's records, which tell the turret
    tables of one vehicle apart.
    """
    _turret_tables(data, spans, out)
    for obj in props.iter_objects(data):
        t = obj.type
        if t == 'WeaponData':
            wid = obj.string('id')
            if not wid:
                continue
            att = []
            f = obj.fields.get('attachment_list')
            if f:
                att = [a.token('name') for a in props.iter_objects(f[1], type_name='WeaponAttachment')]
            out['weapons'].setdefault(wid, dict(model=obj.token('model_name') or 0,
                                                sight=obj.token('required_sight_attachment') or 0,
                                                attachments=[a for a in att if a]))
        elif t == 'AttachmentData':
            aid = obj.string('id')
            if aid:
                out['attachments'].setdefault(token(aid), dict(id=aid, type=_enum(obj, 'attachment_type'),
                                                               model=obj.token('model_name') or 0))
        elif t == 'StreamedItemData':
            sid, m = obj.string('id'), obj.token('model_name')
            if sid and m:
                if stem.startswith('streamed_weapons'):
                    out['fp_weapons'].setdefault(sid, m)
                elif stem.startswith('streamed_attachments'):
                    out['fp_attachments'].setdefault(sid, m)
        elif t == 'VehicleData':
            m, ph = obj.token('ModelFileName'), obj.token('PropertiesFileName')
            if m and ph:
                out['vehicles'].setdefault(m, ph)
        elif t == 'CharacterHeadDataTable' and out['heads'] is None:
            m = obj.token('head_model_name')
            f = obj.fields.get('CharacterHeadDataList')
            if not m or not f:
                continue
            tones = {}
            for h in props.iter_objects(f[1], type_name='CharacterHeadData'):
                i = _u8(h, 'head_model_index')
                if i is not None:
                    tones[i] = _enum(h, 'skin_tone')
            out['heads'] = dict(model=m, tones=[tones.get(i) for i in range(max(tones) + 1)] if tones else [])
    return out


def wants_scan(data):
    return any(k in data for k in (b'WeaponData\0', b'AttachmentData\0', b'StreamedItemData\0',
                                   b'CharacterHeadDataTable\0', b'VehicleData\0', b'TurretProperties\0'))


def merge(dst, src):
    for k in ('weapons', 'attachments', 'fp_weapons', 'fp_attachments', 'vehicles', 'turrets'):
        for key, v in src[k].items():
            dst[k].setdefault(key, v)
    if dst['heads'] is None:
        dst['heads'] = src['heads']
    return dst


def vehicle_weapons(lo, model_hash):
    """[(seat node hash, [weapon model hash, ...])] for a vehicle model: the weapons of the turret
    each seat operates, by WeaponData token (lowercase id). Weapons without a model (built into
    the vehicle, like the Hind's ``hind_chin``) are left out. A seat node of 0 means the seat has
    no node of its own (deployable turrets)."""
    t = lo['turrets'].get(lo['vehicles'].get(model_hash))
    if not t:
        return []
    by_tok = {}
    for wid, w in lo['weapons'].items():
        if w['model']:
            by_tok.setdefault(token(wid.lower()), w['model'])
            by_tok.setdefault(token(wid), w['model'])
    out = []
    for node, ti in t['seats']:
        if 0 <= ti < len(t['weapons']):
            models = [by_tok[w] for w in t['weapons'][ti] if w in by_tok]
            if models:
                out.append((node, models))
    return out


def weapon_for_model(lo, model_hash):
    """(weapon id, first_person) of a weapon model hash, or (None, False)."""
    for wid, w in lo['weapons'].items():
        if w['model'] == model_hash:
            return wid, False
    for wid, m in lo['fp_weapons'].items():
        if m == model_hash:
            return wid, True
    return None, False


def weapon_attachments(lo, wid, first_person):
    """[(attachment id, type, socket name, model hash, required)] for a weapon, the required sight
    first. Attachments without a model of the wanted kind are left out."""
    w = lo['weapons'].get(wid)
    if w is None:
        return []
    order = [w['sight']] + [a for a in w['attachments'] if a != w['sight']] if w['sight'] else list(w['attachments'])
    out = []
    for tok in order:
        a = lo['attachments'].get(tok)
        if a is None or a['type'] not in SOCKET_FOR_TYPE:
            continue
        m = lo['fp_attachments'].get(a['id'], 0) if first_person else a['model']
        if m:
            out.append((a['id'], a['type'], SOCKET_FOR_TYPE[a['type']], m, tok == w['sight']))
    return out


def model_roles(lo):
    """model hash -> ('WEAPON' | 'ATTACHMENT', first_person) for the catalog."""
    out = {}
    for w in lo['weapons'].values():
        if w['model']:
            out.setdefault(w['model'], ('WEAPON', False))
    for m in lo['fp_weapons'].values():
        out.setdefault(m, ('WEAPON', True))
    for a in lo['attachments'].values():
        if a['model']:
            out.setdefault(a['model'], ('ATTACHMENT', False))
    for m in lo['fp_attachments'].values():
        out.setdefault(m, ('ATTACHMENT', True))
    return out

