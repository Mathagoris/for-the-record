# artcar v3 — scene_manager logic (two zones, macros-only, per-zone presets)
#
# Paste into the Text DAT named 'logic' inside /record_player/scene_manager.
# Changes from v2:
#   - COMMONS removed: the scene contract is Macro1-6 only
#   - LINK removed: every action applies to the viewed zone, no exceptions
#   - Presets are PER-ZONE banks: P1-4 save/recall for the viewed zone only
#   - Per-zone react source: each zone reacts to low/mid/high/beat
#     independently (/scene/react/1..4, echo-lit like slots)
#   - FLASH (/perform/flash): car-wide effect trigger for drops/crescendos
#   - TAP moved to page 2, still /perform/tap; guarded until beat1 exists
#
# New ops this expects (create them, or the guards will debug-log and skip):
#   /record_player/scene_manager/player_dance/react_dance   Select CHOP: input = audio_info,
#   /record_player/scene_manager/player_body/react_body      rename to 'react'; logic sets
#                                                which channel it selects
#   /record_player/scene_manager/flash_trigger Trigger CHOP (attack ~0,
#                                                decay ~0.4s); both players'
#                                                brightness adds its output
#   /record_player/audio/audio_info              Beat CHOP (for tap + 'beat'
#                                                react source) — when built
#
# State in MGR storage: viewedZone, savemode,
#   {zone}_slot, {zone}_deck, {zone}_reactsrc

import json, os

# ---------------------------------------------------------------- PATHS
MGR    = op('/record_player/scene_manager')
OUT    = op('/record_player/OSC/oscout_dat')
GLOBAL = op('/record_player/scene_manager/global')

ZONES = ('dance', 'body')
SLOT_TABLE = {'dance': '/record_player/scene_manager/slots_dance',
              'body':  '/record_player/scene_manager/slots_body'}
PLAYER = {'dance': '/record_player/scene_manager/player_dance',
          'body':  '/record_player/scene_manager/player_body'}
REACT_SELECT = {'dance': '/record_player/scene_manager/player_dance/react_dance',
                'body':  '/record_player/scene_manager/player_body/react_body'}
FLASH_TRIGGER = '/record_player/scene_manager/flash_trigger'
BEAT_CHOP = '/record_player/audio/audio_info'

REACT_SRC = {1: 'kick', 2: 'snare', 3: 'tap_tempo'}
GLOBAL_CH = {'master': 0, 'react': 1, 'blackout': 2}
N_SLOTS, N_MACROS = 9, 6

# ---------------------------------------------------------------- helpers
def clamp(v):
    return max(0.0, min(1.0, float(v)))

def Send(addr, args):
    try:
        OUT.sendOSC(addr, args)
    except Exception as e:
        debug('OSC send failed', addr, e)

def Table(zone):
    return op(SLOT_TABLE[zone])

def SlotCount(zone):
    t = Table(zone)
    return (t.numRows - 1) if t else 0

def ScenePath(zone, slot):
    t = Table(zone)
    return t[slot, 'path'].val if t and 0 < slot <= SlotCount(zone) else None

def SceneOp(zone, slot):
    p = ScenePath(zone, slot)
    return op(p) if p else None

def ActiveSlot(zone):
    return MGR.fetch(f'{zone}_slot', 1)

def ActiveScene(zone):
    return SceneOp(zone, ActiveSlot(zone))

def Viewed():
    return MGR.fetch('viewedZone', 'dance')

# ---------------------------------------------------------------- zone view
def SetViewedZone(zone):
    if zone not in ZONES:
        return
    MGR.store('viewedZone', zone)
    Send('/ui/zone/1', [1.0 if zone == 'dance' else 0.0])
    Send('/ui/zone/2', [1.0 if zone == 'body' else 0.0])
    PushSceneState()          # full re-skin: the surface changes allegiance

# ---------------------------------------------------------------- switching
def SelectSlot(zone, slot):
    """Slot-index semantics: each zone resolves the slot via its own table."""
    slot = int(slot)
    s = SceneOp(zone, slot)
    if s is None:
        debug(f'{zone}: slot {slot} empty or missing scene — ignored')
        return
    if slot == ActiveSlot(zone):
        return
    s.allowCooking = True
    ply = op(PLAYER[zone])
    deck = MGR.fetch(f'{zone}_deck', 'A')
    if deck == 'A':
        ply.op('deck_b').par.pop = ScenePath(zone, slot) + '/out'
        ply.op('fade_target').par.value0 = 1.0
        MGR.store(f'{zone}_deck', 'B')
    else:
        ply.op('deck_a').par.pop = ScenePath(zone, slot) + '/out'
        ply.op('fade_target').par.value0 = 0.0
        MGR.store(f'{zone}_deck', 'A')
    MGR.store(f'{zone}_slot', slot)
    delay = int(MGR.par.Fadetime * me.time.rate) + 5
    run(f"op('/record_player/scene_manager/logic').module.SleepIdle('{zone}')",
        delayFrames=delay)

def SleepIdle(zone):
    """After a fade, only scenes on this zone's decks keep cooking."""
    ply = op(PLAYER[zone])
    if ply is None:
        return
    onDecks = {ply.op('deck_a').par.pop.eval(), ply.op('deck_b').par.pop.eval()}
    t = Table(zone)
    for r in range(1, t.numRows):
        s = op(t[r, 'path'].val)
        if s:
            s.allowCooking = (t[r, 'path'].val + '/out') in onDecks

# ---------------------------------------------------------------- react src
def ReactSource(zone):
    return MGR.fetch(f'{zone}_reactsrc', 1)

def _applyReactSelect(zone):
    """Point the zone's Select CHOP at the chosen audio_info channel."""
    sel = op(REACT_SELECT[zone])
    if sel is None:
        debug(f'{zone}: react select CHOP missing — source stored only')
        return
    try:
        sel.par.channames = REACT_SRC[ReactSource(zone)]
        sel.par.renamefrom = '*'
        sel.par.renameto = 'react'
    except Exception as e:
        debug(f'{zone}: react select pars failed: {e}')

def SetReactSource(idx):
    idx = int(idx)
    if idx not in REACT_SRC:
        return
    zone = Viewed()
    MGR.store(f'{zone}_reactsrc', idx)
    _applyReactSelect(zone)
    _pushReactSource(zone)

def _pushReactSource(zone):
    src = ReactSource(zone)
    for n in REACT_SRC:
        Send(f'/scene/react/{n}', [1.0 if n == src else 0.0])

# ---------------------------------------------------------------- feedback
def PushSceneState():
    """Re-skin the tablet to the VIEWED zone's truth: name, slot labels,
    exclusive slot lights, macro labels + values, react source, preset bank."""
    zone = Viewed()
    idx, s = ActiveSlot(zone), ActiveScene(zone)
    t = Table(zone)
    if s is None or t is None:
        return
    Send('/scene/name', [f'{zone.upper()} - {s.name}'])
    for n in range(1, N_SLOTS + 1):
        nm = t[n, 'name'].val if n <= SlotCount(zone) else '-'
        Send(f'/perform/scene/{n}/label', [nm])
        Send(f'/perform/scene/{n}', [1.0 if n == idx else 0.0])
    for i in range(1, N_MACROS + 1):
        Send(f'/macro/{i}/label', [s.par[f'Label{i}'].eval()])
        Send(f'/macro/{i}/value', [float(s.par[f'Macro{i}'])])
    _pushReactSource(zone)
    Send('/preset/zonelabel', [f'PRESETS - {zone.upper()}'])
    Send('/meter/zonelabel', [f'BEAT REACT - {zone.upper()}'])

# ---------------------------------------------------------------- writes
def SetGlobal(name, v):
    v = clamp(v)
    setattr(GLOBAL.par, f'value{GLOBAL_CH[name]}', v)
    Send(f'/perform/{name}', [v])

def ApplyMacro(i, v):
    i, v = int(i), clamp(v)
    if not 1 <= i <= N_MACROS:
        return
    s = ActiveScene(Viewed())
    if s:
        setattr(s.par, f'Macro{i}', v)
    Send(f'/macro/{i}/value', [v])

def TapSlot(slot):
    SelectSlot(Viewed(), slot)
    PushSceneState()

def Home():
    """Whole car to known-good: both zones slot 1, lights on."""
    SetGlobal('blackout', 0.0)
    SetGlobal('master', 0.8)
    for zone in ZONES:
        SelectSlot(zone, 1)
    PushSceneState()

def Flash():
    """Car-wide effect burst for drops / crescendos."""
    t = op(FLASH_TRIGGER)
    if t is None:
        debug('flash trigger CHOP missing — flash ignored')
        return
    try:
        t.par.triggerpulse.pulse()
    except Exception as e:
        debug('flash trigger pulse failed:', e)

def Tap():
    b = op(BEAT_CHOP)
    if b is None:
        debug('beat CHOP not built yet — tap ignored')
        return
    try:
        b.par.tap.pulse()
    except Exception as e:
        debug('beat CHOP tap failed:', e)

# ---------------------------------------------------------------- presets
# Per-zone banks: { 'dance': {'1': snap, ...}, 'body': {...} }
def _presetPath():
    return os.path.join(project.folder, 'artcar_presets.json')

def _loadAll():
    try:
        with open(_presetPath()) as f:
            return json.load(f)
    except Exception:
        return {}

def PresetTouched(slot):
    if MGR.fetch('savemode', 0):
        SavePreset(slot)
        MGR.store('savemode', 0)
        Send('/preset/savemode', [0.0])
    else:
        RecallPreset(slot)

def SavePreset(slot):
    """Snapshot the VIEWED zone only into that zone's bank."""
    zone = Viewed()
    s = ActiveScene(zone)
    if s is None:
        return
    allp = _loadAll()
    bank = allp.setdefault(zone, {})
    bank[str(int(slot))] = {
        'slot': ActiveSlot(zone),
        'macros': [float(s.par[f'Macro{i}']) for i in range(1, N_MACROS + 1)],
        'reactsrc': ReactSource(zone),
    }
    with open(_presetPath(), 'w') as f:
        json.dump(allp, f, indent=2)
    debug(f'{zone} preset saved ->', slot)

def RecallPreset(slot):
    """Restore the VIEWED zone from its own bank; other zone untouched."""
    zone = Viewed()
    p = _loadAll().get(zone, {}).get(str(int(slot)))
    if not p:
        debug(f'{zone} preset', slot, 'is empty')
        return
    SelectSlot(zone, p['slot'])
    s = ActiveScene(zone)
    if s is not None:
        for i, v in enumerate(p.get('macros', []), 1):
            setattr(s.par, f'Macro{i}', clamp(v))
    MGR.store(f'{zone}_reactsrc', int(p.get('reactsrc', 1)))
    _applyReactSelect(zone)
    PushSceneState()

# ---------------------------------------------------------------- router
def Handle(address, args):
    a = list(args) if args else []
    v = float(a[0]) if a else 1.0
    parts = address.strip('/').split('/')
    if not parts:
        return

    if parts[0] == 'ui':
        if parts[1] == 'zone' and len(parts) > 2 and v > 0.5:
            SetViewedZone('dance' if parts[2] == '1' else 'body')

    elif parts[0] == 'perform':
        what = parts[1]
        if what == 'scene' and len(parts) > 2:
            if v > 0.5:
                TapSlot(int(parts[2]))
            else:
                # momentary release: restate truth so the light doesn't die
                n = int(parts[2])
                Send(f'/perform/scene/{n}',
                     [1.0 if n == ActiveSlot(Viewed()) else 0.0])
        elif what in ('master', 'react', 'blackout'):
            SetGlobal(what, v)
        elif what == 'home' and v > 0.5:
            Home()
        elif what == 'flash' and v > 0.5:
            Flash()
        elif what == 'tap' and v > 0.5:
            Tap()

    elif parts[0] == 'scene' and len(parts) > 2 and parts[1] == 'react':
        if v > 0.5:
            SetReactSource(int(parts[2]))
        else:
            _pushReactSource(Viewed())   # momentary release: restate

    elif parts[0] == 'macro' and len(parts) > 2 and parts[2] == 'value':
        ApplyMacro(parts[1], v)

    elif parts[0] == 'preset':
        if parts[1] == 'recall':
            PresetTouched(int(v))        # slot number rides in the argument
        elif parts[1] == 'savemode':
            MGR.store('savemode', int(v > 0.5))
            Send('/preset/savemode', [float(v > 0.5)])

def Init():
    """Run once: op('/record_player/scene_manager/logic').module.Init()"""
    MGR.store('viewedZone', 'dance')
    MGR.store('savemode', 0)
    for zone in ZONES:
        MGR.store(f'{zone}_slot', 1)
        MGR.store(f'{zone}_deck', 'A')
        MGR.store(f'{zone}_reactsrc', 1)
        _applyReactSelect(zone)
    Home()
    SetViewedZone('dance')
