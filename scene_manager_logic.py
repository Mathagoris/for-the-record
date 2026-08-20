# artcar v6 — scene_manager logic (three real zones + output switch)
#
# Paste into the Text DAT named 'logic' inside /record_player/scene_manager.
# Changes from v5:
#   - FULL is now a REAL third pipeline, not a mirrored view:
#       pool_full    scenes that render the ENTIRE car point cloud in one
#                    POP — one noise field, one clock, dancefloor and body
#                    inherently in sync
#       player_full  third deck player, clone of the other two
#       slots_full   Table DAT, now  name | path  exactly like slots_dance
#     No more mirrored macro writes: a FULL scene is one scene, one
#     instance. Macros/react in FULL view hit player_full's active scene
#     only. (The v5 name|dance|body slots_full schema is obsolete —
#     rebuild the table as name|path pointing into pool_full.)
#   - OUTPUT SWITCH: the car's output is either the split pair
#     (player_dance + player_body) or player_full, never both. Logic
#     writes 0 (split) / 1 (full) to the switch CHOP; you wire that to
#     the Switch/cross at the output stage.
#     FIRING A SCENE CLAIMS THE OUTPUT: tap or preset-recall in FULL view
#     -> output flips to full; tap or recall in dance/body view -> output
#     flips to split. Just *viewing* a zone or moving its macros never
#     flips anything, so you can pre-stage the other branch silently.
#     The scene name shows "[staged]" when the viewed zone's branch is
#     not the live output.
#   - Branch power management: on an output flip the incoming branch's
#     deck scenes wake before the switch moves, and the outgoing branch's
#     scenes go to sleep one Fadetime later (safe if you crossfade the
#     switch). SleepIdle is back to a per-zone table sweep.
#   - Presets: three banks ('dance','body','full'), all the same schema
#     {'path','slot','macros','reactsrc'}. Recall fires the scene, so it
#     also claims the output for that bank's branch. Any 'full' bank
#     saved under v5 used the old schema and is ignored.
#
# TD wiring this expects (new since v5):
#   - /record_player/pool_full            scenes over the full-car points
#   - /record_player/scene_manager/player_full
#       clone of player_dance: deck_a, deck_b, fade_target, plus a Select
#       CHOP named react_full inside (input audio_info, renamed 'react')
#   - /record_player/scene_manager/slots_full  Table DAT: name | path
#   - /record_player/scene_manager/output_switch
#       Constant CHOP, value0: 0 = split (dance+body), 1 = full.
#       Wire to the output-stage Switch POP / crossfade.
#   - player_full brightness = master (x flash); the int_dance/int_body
#     trims only shape the split branch
#   Still outstanding from earlier: GLOBAL value3/value4 channels wired
#   as master x zone trim on the split players; beat CHOP.
#
# State in MGR storage: viewedZone, output ('split'|'full'), savemode,
#   {zone}_path, {zone}_deck, {zone}_reactsrc   for zone in dance/body/full

import json, os

# ---------------------------------------------------------------- PATHS
MGR    = op('/record_player/scene_manager')
OUT    = op('/record_player/OSC/oscout_dat')
GLOBAL = op('/record_player/scene_manager/global')

ZONES = ('dance', 'body', 'full')        # three real pipelines
SPLIT_ZONES = ('dance', 'body')
ZONE_NUM = {'dance': 1, 'body': 2, 'full': 3}
BRANCH = {'dance': 'split', 'body': 'split', 'full': 'full'}
SLOT_TABLE = {'dance': '/record_player/scene_manager/slots_dance',
              'body':  '/record_player/scene_manager/slots_body',
              'full':  '/record_player/scene_manager/slots_full'}
PLAYER = {'dance': '/record_player/scene_manager/player_dance',
          'body':  '/record_player/scene_manager/player_body',
          'full':  '/record_player/scene_manager/player_full'}
REACT_SELECT = {'dance': '/record_player/scene_manager/player_dance/react_dance',
                'body':  '/record_player/scene_manager/player_body/react_body',
                'full':  '/record_player/scene_manager/player_full/react_full'}
OUTPUT_SWITCH = '/record_player/scene_manager/output_switch'
FLASH_TRIGGER = '/record_player/scene_manager/flash_trigger'
BEAT_CHOP = '/record_player/audio/audio_info'

REACT_SRC = {1: 'kick', 2: 'snare', 3: 'tap_tempo'}
GLOBAL_CH = {'master': 0, 'react': 1, 'blackout': 2,
             'int_dance': 3, 'int_body': 4}
N_SLOTS, N_MACROS = 9, 6

# ---------------------------------------------------------------- helpers
def clamp(v):
    return max(0.0, min(1.0, float(v)))

def Send(addr, args):
    try:
        OUT.sendOSC(addr, args)
    except Exception as e:
        debug('OSC send failed', addr, e)

def _deckPath(deck):
    """A POP parameter evaluates to an OP object, not a path string.
    Normalize to an absolute path ('' when the deck is empty)."""
    if deck is None:
        return ''
    try:
        v = deck.par.pop.eval()
    except Exception:
        return ''
    if v is None:
        return ''
    return v.path if hasattr(v, 'path') else str(v)

def Table(zone):
    return op(SLOT_TABLE[zone])

def SlotCount(zone):
    t = Table(zone)
    return (t.numRows - 1) if t else 0

def ScenePath(zone, slot):
    t = Table(zone)
    return t[slot, 'path'].val if t and 0 < slot <= SlotCount(zone) else None

def ActivePath(zone):
    return MGR.fetch(f'{zone}_path', None)

def ActiveScene(zone):
    p = ActivePath(zone)
    return op(p) if p else None

def ZoneSlot(zone):
    """Row in the zone's table matching its active path, else 0."""
    p, t = ActivePath(zone), Table(zone)
    if not p or not t:
        return 0
    for r in range(1, t.numRows):
        if t[r, 'path'].val == p:
            return r
    return 0

def Viewed():
    return MGR.fetch('viewedZone', 'dance')

def Output():
    return MGR.fetch('output', 'split')

def ViewIsLive(zone=None):
    return BRANCH[zone or Viewed()] == Output()

# ---------------------------------------------------------------- zone view
def SetViewedZone(zone):
    if zone not in ZONES:
        return
    MGR.store('viewedZone', zone)
    for z, n in ZONE_NUM.items():
        Send(f'/ui/zone/{n}', [1.0 if z == zone else 0.0])
    PushSceneState()          # full re-skin: the surface changes allegiance

# ---------------------------------------------------------------- output
def _branchScenes(branch):
    """All scene paths reachable by a branch's tables."""
    zones = SPLIT_ZONES if branch == 'split' else ('full',)
    paths = set()
    for z in zones:
        t = Table(z)
        if t:
            for r in range(1, t.numRows):
                paths.add(t[r, 'path'].val)
    return paths

def _deckScenes(branch):
    """Scene paths currently loaded on a branch's decks."""
    zones = SPLIT_ZONES if branch == 'split' else ('full',)
    onDecks = set()
    for z in zones:
        ply = op(PLAYER[z])
        if ply:
            for d in ('deck_a', 'deck_b'):
                p = _deckPath(ply.op(d))
                if p:
                    onDecks.add(p[:-4] if p.endswith('/out') else p)
    return onDecks

def SetOutput(mode):
    """Flip the car between the split pair and player_full. Wakes the
    incoming branch's deck scenes first, moves the switch, then puts the
    outgoing branch to sleep one Fadetime later."""
    if mode not in ('split', 'full') or mode == Output():
        return
    for p in _deckScenes(mode):              # wake what's about to be seen
        s = op(p)
        if s:
            s.allowCooking = True
    sw = op(OUTPUT_SWITCH)
    if sw is None:
        debug('output_switch CHOP missing — output NOT flipped')
        return
    try:
        sw.par.index = 1.0 if mode == 'full' else 0.0
    except Exception as e:
        debug('output_switch write failed:', e)
        return
    old = Output()
    MGR.store('output', mode)
    delay = int(MGR.par.Fadetime * me.time.rate) + 5
    run(f"op('/record_player/scene_manager/logic').module.SleepBranch('{old}')",
        delayFrames=delay)

def SleepBranch(branch):
    """Outgoing branch is dark: stop cooking everything it references.
    No-op if the output flipped back during the fade."""
    if branch == Output():
        return
    for p in _branchScenes(branch):
        s = op(p)
        if s:
            s.allowCooking = False

# ---------------------------------------------------------------- switching
def SelectScenePath(zone, path):
    """Core switch: crossfade the zone's player to the scene at `path`."""
    s = op(path) if path else None
    if s is None:
        debug(f'{zone}: scene missing at {path} — ignored')
        return
    if path == ActivePath(zone):
        return
    s.allowCooking = True
    ply = op(PLAYER[zone])
    if ply is None:
        debug(f'{zone}: player missing at {PLAYER[zone]} — ignored')
        return
    deck = MGR.fetch(f'{zone}_deck', 'A')
    if deck == 'A':
        ply.op('deck_b').par.pop = path + '/out'
        ply.op('fade_target').par.value0 = 1.0
        MGR.store(f'{zone}_deck', 'B')
    else:
        ply.op('deck_a').par.pop = path + '/out'
        ply.op('fade_target').par.value0 = 0.0
        MGR.store(f'{zone}_deck', 'A')
    MGR.store(f'{zone}_path', path)
    delay = int(MGR.par.Fadetime * me.time.rate) + 5
    run(f"op('/record_player/scene_manager/logic').module.SleepIdle('{zone}')",
        delayFrames=delay)

def SelectSlot(zone, slot):
    SelectScenePath(zone, ScenePath(zone, int(slot)))

def SleepIdle(zone):
    """After a fade, only scenes on this zone's decks keep cooking.
    Skips the sweep entirely while the zone's branch is dark — the
    branch-level sleep owns that state."""
    if BRANCH[zone] != Output():
        return
    ply = op(PLAYER[zone])
    if ply is None:
        return
    onDecks = {_deckPath(ply.op('deck_a')), _deckPath(ply.op('deck_b'))}
    t = Table(zone)
    if not t:
        return
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
def DisplaySlot():
    return ZoneSlot(Viewed())

def PushSceneState():
    """Re-skin the tablet to the viewed zone's truth. All three zones are
    uniform now. '[staged]' flags a zone whose branch isn't the live
    output — you're editing in the dark, on purpose or not."""
    zone = Viewed()
    s, t = ActiveScene(zone), Table(zone)
    if s is None or t is None:
        return
    staged = '' if ViewIsLive() else '  [staged]'
    Send('/scene/name', [f'{zone.upper()} - {s.name}{staged}'])
    lit = DisplaySlot()
    for n in range(1, N_SLOTS + 1):
        nm = t[n, 'name'].val if n <= SlotCount(zone) else '-'
        Send(f'/perform/scene/{n}/label', [nm])
        Send(f'/perform/scene/{n}', [1.0 if n == lit else 0.0])
    for i in range(1, N_MACROS + 1):
        Send(f'/macro/{i}/label', [s.par[f'Label{i}'].eval()])
        Send(f'/macro/{i}/value', [float(s.par[f'Macro{i}'])])
    _pushReactSource(zone)
    Send('/preset/zonelabel', [f'PRESETS - {zone.upper()}'])
    Send('/meter/zonelabel', [f'BEAT REACT - {zone.upper()}'])

# ---------------------------------------------------------------- writes
def SetGlobal(name, v):
    v = clamp(v)
    try:
        setattr(GLOBAL.par, f'value{GLOBAL_CH[name]}', v)
    except Exception as e:
        debug(f'global channel {name} missing on GLOBAL chop:', e)
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
    """Fire a scene in the viewed zone AND claim the output for its
    branch. Tapping in FULL takes the whole car; tapping in dance or
    body hands the car back to the split pair."""
    zone = Viewed()
    path = ScenePath(zone, int(slot))
    if not path:
        debug(f'{zone}: slot {slot} empty — ignored')
        return
    SelectScenePath(zone, path)
    SetOutput(BRANCH[zone])
    PushSceneState()

def Home():
    """Whole car to known-good: split output, both zones slot 1,
    lights on, trims open."""
    SetGlobal('blackout', 0.0)
    SetGlobal('master', 0.8)
    SetGlobal('int_dance', 1.0)
    SetGlobal('int_body', 1.0)
    SelectSlot('full', 1)
    SetOutput('full')
    SetViewedZone('full')

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
# Banks: { 'dance': {...}, 'body': {...}, 'full': {...} } — one schema:
# {'path', 'slot', 'macros', 'reactsrc'}. Recall is a fire, so it claims
# the output for the bank's branch, same as a pad tap.
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
    zone = Viewed()
    s = ActiveScene(zone)
    if s is None:
        return
    allp = _loadAll()
    bank = allp.setdefault(zone, {})
    bank[str(int(slot))] = {
        'path': ActivePath(zone),
        'slot': ZoneSlot(zone),
        'macros': [float(s.par[f'Macro{i}']) for i in range(1, N_MACROS + 1)],
        'reactsrc': ReactSource(zone),
    }
    with open(_presetPath(), 'w') as f:
        json.dump(allp, f, indent=2)
    debug(f'{zone} preset saved ->', slot)

def RecallPreset(slot):
    zone = Viewed()
    p = _loadAll().get(zone, {}).get(str(int(slot)))
    if not p or not (p.get('path') or p.get('slot')):
        debug(f'{zone} preset', slot, 'is empty or incompatible')
        return
    path = p.get('path')
    if path:
        SelectScenePath(zone, path)
    else:
        SelectSlot(zone, p.get('slot', 1))
    s = ActiveScene(zone)
    if s is not None:
        for i, v in enumerate(p.get('macros', []), 1):
            setattr(s.par, f'Macro{i}', clamp(v))
    MGR.store(f'{zone}_reactsrc', int(p.get('reactsrc', 1)))
    _applyReactSelect(zone)
    SetOutput(BRANCH[zone])
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
            SetViewedZone({'1': 'dance', '2': 'body', '3': 'full'}
                          .get(parts[2], 'dance'))

    elif parts[0] == 'perform':
        what = parts[1]
        if what == 'scene' and len(parts) > 2:
            if v > 0.5:
                TapSlot(int(parts[2]))
            else:
                # momentary release: restate truth so the light doesn't die
                n = int(parts[2])
                Send(f'/perform/scene/{n}', [1.0 if n == DisplaySlot() else 0.0])
        elif what in ('master', 'react', 'blackout', 'int_dance', 'int_body'):
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
    MGR.store('output', 'split')
    MGR.store('savemode', 0)
    for zone in ZONES:
        MGR.store(f'{zone}_path', None)
        MGR.store(f'{zone}_deck', 'A')
        MGR.store(f'{zone}_reactsrc', 1)
        _applyReactSelect(zone)
    sw = op(OUTPUT_SWITCH)
    if sw is not None:
        try:
            sw.par.index = 0.0
        except Exception as e:
            debug('output_switch init failed:', e)
    SelectSlot('full', 1)      # pre-stage a full scene so the view isn't empty
    Home()
    SleepBranch('full')        # dark branch starts asleep; SetOutput wakes it
    SetViewedZone('dance')