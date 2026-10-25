"""Self-test: python selftest.py <device-exported.t8k>  (no TR-8S needed)
1. reading + rewriting a real export must be byte-identical (proves checksums/offsets),
2. a new kit (mono/stereo/ROM/22.05 kHz pads, colors) must read back unchanged,
3. pad rendering (markers, fades, de-click, filters, mono, Fit, 22k) and the fit-to-RAM logic behave,
4. SD export writes the kit and its samples to the two chosen folders."""
import hashlib
import math
import os
import sys
import tempfile

import numpy as np

import app
import t8k

# USB protocol encoding (Roland SysEx), checked against addresses from Roland's own TR-8S config
import tr8s_usb as usb  # noqa: E402
assert usb.enc7(624, 2) == [4, 112] and usb.dec7([4, 112]) == 624 and usb.nibbles([0, 2, 7, 0]) == 0x270
assert usb.offset(usb.offset(usb.TONE_CATEGORY[0], usb.BLOCK, 685), [0]) == [0x35, 45, 0, 0x10]  # US 62 category
assert usb.offset(usb.offset(usb.KIT_TONE[0], usb.BLOCK, 6), [0x01, 0x00], 10) == [0x10, 6, 26, 0]  # kit 7, RC
assert usb.offset(usb.UTILITY, [usb.COMMAND['deleteTone']]) == [0x50, 0, 0, 0x24]
blob = os.urandom(1312)
assert usb.unpack7(usb.pack7(list(blob))) == blob and usb.pack7([0x80, 1, 0xff]) == [0b101, 0, 1, 0x7f]
# the kit data sent over USB is the .t8k KIT data with the pad tone ids, colors and name filled in
body = t8k.kit_body('usb kit', [624, 635] + [27] * 9, [9] * 11, bytes(1312))
assert len(body) == 1312 and body[:7] == b'USB KIT' and body[0x184:0x186] == (624).to_bytes(2, 'little')
msg = usb.TR8S.__new__(usb.TR8S)
msg.dev = 0x10
assert msg._sysex(usb.DT1, [0x50, 0, 0, 0x13], [0]) == [0xF0, 0x41, 0x10, 0, 0, 0, 0x45, 0x12, 0x50, 0, 0, 0x13, 0,
                                                        usb.checksum([0x50, 0, 0, 0x13, 0]), 0xF7]

tmp = tempfile.mkdtemp()
src = sys.argv[1]
name, slots, meta = t8k.read(src)
out = os.path.join(tmp, os.path.basename(src))
t8k.write(out, name, slots, **meta)
assert open(out, 'rb').read() == open(src, 'rb').read(), 't8k mismatch'
txt = os.path.splitext(src)[0] + '.txt'
if os.path.exists(txt):
    assert open(out[:-4] + '.txt', 'rb').read() == open(txt, 'rb').read(), 'txt mismatch'

rng = np.random.default_rng(0)
mono, stereo = rng.integers(-32768, 32767, (1000, 1), dtype='<i2'), rng.integers(-32768, 32767, (300000, 2), dtype='<i2')
new = [('kick', mono, 44100), ('snare', stereo, 22050), ('rom', 0x10, None)] + [(f's{i}', mono, 44100) for i in range(8)]
colors = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
t8k.write(os.path.join(tmp, 'new.t8k'), 'my kit', new, kit=meta['kit'], colors=colors)
try:  # no kit parameters ship with the app: a kit file needs an opened kit's or the TR-8S's
    t8k.write(os.path.join(tmp, 'x.t8k'), 'x', new)
    raise AssertionError('kit file written without kit parameters')
except ValueError:
    pass
n2, s2, m2 = t8k.read(os.path.join(tmp, 'new.t8k'))
assert m2['colors'] == colors and meta['colors'] == t8k.DEFAULT_COLORS
assert n2 == 'MY KIT' and s2[0][0] == 'KICK' and s2[2][1] == 0x10 and s2[1][2] == 22050
assert all(np.array_equal(a[1], b[1]) for a, b in zip(new, s2))

# an untouched kit sample exports bit-identical (needed for the TR-8S to recognise it as the same sample)
s = app.Slot()
s.set('orig', slots[1][1].astype('float32') / 32768)
assert np.array_equal(s.render()[0], slots[1][1])

# pad rendering: everything non-destructive, applied on export
ones = np.full((44100, 2), 0.5, 'float32')
s = app.Slot()
s.set('pad', ones)
s.start, s.end, s.fade_in, s.fade_out = 100, 22150, 10, 10
x, rate = s.render()
assert rate == 44100 and x.shape == (22050, 2) and x[0, 0] == 0 and x[-1, 0] == 0 and x[441, 0] > 16000
assert np.array_equal(s.audio, ones)  # source untouched

# Fit: exactly one 128 KB block for the pad's current Mono / 22k settings, ending on the zero line
loud = np.sin(np.arange(44100 * 5) * 2 * np.pi * 110 / 44100).astype('float32')[:, None].repeat(2, 1) * 0.9
s.set('long', loud)
s.fit = True
for mono, lo, seconds in ((False, False, 0.743), (True, False, 1.486), (True, True, 2.972)):
    s.mono, s.lo = mono, lo
    x, rate = s.render()
    assert app.lib.cost(x.size * 2) == 131072 and x.size * 2 == s.nbytes(), (mono, lo, x.shape, s.nbytes())
    assert abs(s.length() / 44100 - seconds) < 0.001 and np.all(x[-1] == 0) and abs(int(x[-200, 0])) > 0
s.fit = s.mono = s.lo = False
assert s.nbytes() == 44100 * 5 * 4  # Fit off: the whole sound

# de-click: markers that cut into a loud sound still start and end exactly on the axis (S-curve, 3 ms)
s.start, s.end = 1000, 50000
x, _ = s.render()
assert np.all(x[0] == 0) and np.all(x[-1] == 0) and np.abs(x[300]).max() > 20000  # only the first ~3 ms touched
s.set('own end', loud[:-37])  # untouched, but the sound's own last frame is far off the axis: de-clicked too
x, _ = s.render()
assert np.all(x[-1] == 0) and abs(int(loud[-38, 0] * 32768)) > 1000
f = app.fade(np.ones((441, 1), 'float32'), 10, False)[:, 0]
assert f[0] == 0 and abs(f[-1] - 1) < 1e-6 and f[1] < 0.001 and abs(f[220] - 0.5) < 0.01  # S-curve, not a line
sine = np.sin(np.arange(44100) * 2 * np.pi * 5000 / 44100).astype('float32')[:, None] * 0.9
s.set('sine', sine)
s.lpf = 200.0
assert np.abs(s.render()[0][4410:]).max() < 400
s.lpf, s.hpf = app.LPF_OFF, 200.0
assert np.abs(s.render()[0][4410:]).max() > 25000
s.hpf, s.gain = app.HPF_OFF, -math.inf
assert not s.render()[0].any()
s.gain, s.norm = 0.0, True
s.audio = sine * 0.25
assert abs(int(np.abs(s.render()[0]).max()) - 32767) <= 1
s.gain = 6.0  # +6 dB on a normalized sample clips, never wraps
assert np.abs(s.render()[0]).max() == 32767
assert abs(app.pos_to_db(app.db_to_pos(0.0))) < 1e-9 and app.pos_to_db(0) == -math.inf and app.pos_to_db(1) == 6.0
assert app.loud_range(np.pad(ones, ((100, 100), (0, 0)))) == (100, 44200)
assert len(app.resample(np.zeros((48000, 1), 'float32'), 48000)) == 44100
assert app.fmt_time(0) == 'Off' and app.fmt_time(1500) == '1.50 s' and app.fmt_hz(20000, app.LPF_OFF) == 'Off'

# fit-to-RAM: Cut only touches pads that have it on; then only the largest pads get downsampled (user says yes)
app.messagebox.askyesno = lambda *a, **k: True
app.messagebox.showinfo = lambda *a, **k: None
app.SETTINGS, app.MEMORY = os.path.join(tmp, 'settings.json'), os.path.join(tmp, 'live.json')  # never the user's
app.UPLOADS = os.path.join(tmp, 'uploads.json')
app.KIT_PARAMS = os.path.join(tmp, 'kit_params.bin')  # as if copied from a TR-8S
open(app.KIT_PARAMS, 'wb').write(meta['kit'])
app.play = lambda *a: None
root = app.TkinterDnD.Tk()
root.withdraw()
ui = app.App(root)


def live(free_s, n_tones=0, user_kits=0):
    """A TR-8S state shaped like tr8s_usb.TR8S.read_all() returns it."""
    return dict(version=('3.00', '0CB8'), storage=dict(free_bytes=int(free_s * 88200), longest_free=0),
                tones=[dict(id=624 + i, name='x', size=10, channels=1) for i in range(n_tones)],
                kits=[dict(id=k, name=f'K{k}', tones=[624] + [27] * 10 if k < user_kits else [27] * 11)
                      for k in range(128)])


for i in range(11):
    ui.slots[i].set(f'long{i}', np.zeros((44100 * (10 if i < 2 else 1), 2), 'float32'))
# 50 s left on the TR-8S; in whole 128 KB blocks the kit takes 2 x 20.8 s (10 s stereo) + 9 x 3.0 s (1 s stereo)
# = 68.3 s; downsampling the two long pads brings it to 47.5 s
ui.mem.state = live(50)
name, out_slots = ui.build()
assert [s.lo for s in ui.slots[:3]] == [True, True, False]
assert len(out_slots[0][1]) == 220500 and out_slots[0][2] == 22050
for s in ui.slots:
    s.lo = False
ui.slots[0].fit = ui.slots[1].fit = True  # 2 x 1 block + 9 x 2 blocks = 29.7 s: fits without downsampling
name, out_slots = ui.build()
assert not any(s.lo for s in ui.slots) and len(out_slots[0][1]) == ui.slots[0].fit_frames()
assert len(out_slots[2][1]) == 44100 and ui.slots[2].fit is False
ui.mem.state = None

# sample types from file and folder names
for path, want in [('Pack/Kicks/Big 01.wav', 'Kick'), ('x/BD_909.wav', 'Kick'), ('x/Open Hat 3.wav', 'Open hat'),
                   ('x/Hats/OH_02.wav', 'Open hat'), ('Hats/hat_closed.wav', 'Closed hat'), ('x/Hi-Hat 1.aif', 'Closed hat'),
                   ('Drums/Snare/Tight.wav', 'Snare'), ('x/Clap_808.wav', 'Clap'), ('x/Ride 2.wav', 'Cymbal'),
                   ('Stabs/Dub Chord C.wav', 'Stab'), ('x/Shaker.flac', 'Perc'), ('x/something.wav', 'Other'),
                   ('Kicks/Kick snare layer.wav', 'Kick')]:
    assert app.lib.categorize(path) == want, (path, app.lib.categorize(path))

# library: scan a pack with sub-folders, type chips counted, double-click loads into the selected pad and advances
pack = os.path.join(tmp, 'My Pack')
for rel in ['Kicks/k1.wav', 'Kicks/k2.wav', 'Hats/Closed/ch1.wav', 'Hats/Open/oh1.wav', 'Stabs/stab1.wav', 'notes.txt']:
    os.makedirs(os.path.dirname(os.path.join(pack, rel)), exist_ok=True)
    if rel.endswith('.wav'):
        app.sf.write(os.path.join(pack, rel), rng.uniform(-0.1, 0.1, (4410, 1)), 44100)
    else:
        open(os.path.join(pack, rel), 'w').close()
ui.browser.roots = [pack]
ui.browser.rescan()
types = app.Counter(it['type'] for it in ui.browser.items)
assert len(ui.browser.items) == 5 and types == {'Kick': 2, 'Closed hat': 1, 'Open hat': 1, 'Stab': 1}, types
ui.browser.pick('Kick')
assert len([i for i in ui.browser.tree.get_children(f'd:{pack}|Kicks')]) == 2
ui.select(3)
ui.browser.tree.selection_set([os.path.join(pack, 'Kicks', 'k1.wav'), os.path.join(pack, 'Kicks', 'k2.wav')])
ui.browser.load(ui.sel)
assert ui.slots[3].name == 'k1' and ui.slots[4].name == 'k2' and ui.slots[3].type == 'Kick' and ui.sel == 5

# memory snapshot: live numbers read over USB, kept between sessions; whole 128 KB blocks; limits block export
e1 = ui.slots[3].entry()
assert e1 == dict(name='k1', type='Kick', bytes=131072)
assert app.lib.cost(1) == app.lib.cost(131072) == 131072 and app.lib.cost(131073) == 262144
assert e1['bytes'] == 131072  # a 0.1 s mono sample still takes a whole block
snap = app.lib.Snapshot(os.path.join(tmp, 'snap.json'))
assert snap.usage() == (0, 0, app.Counter(), 0) and snap.state is None
real = live(429.5, 82, 4)  # numbers read from the real TR-8S: 82 samples, 4 kits on them
real['storage']['free_bytes'] = 37879808
snap.update(real)
used, n, _, k = app.lib.Snapshot(snap.path).usage()  # survives a restart
assert used == 15597568 and n == 82 and k == 4
used, n, types, k = snap.usage([e1, e1])  # every import stores its samples again
assert used == 15597568 + 2 * 131072 and n == 84 and k == 5 and types['Kick'] == 2
errors = []
app.messagebox.showerror = lambda *a, **k: errors.append(a)
ui.mem.state = live(500, 395)  # 395 + 11 pads > 400 samples
assert ui.build() is None and '400 samples' in errors[-1][1]
ui.mem.state = live(500, 0, 128)  # all 128 kits already hold user samples
assert ui.build() is None and '128 kits' in errors[-1][1]
ui.mem.state = None

# SD export from real source files living in the SAMPLE folder itself (worst case):
# originals untouched, kit audio == exported WAVs, files flat in EXPORT\KIT and SAMPLE
errors = []
app.messagebox.showerror = lambda *a, **k: errors.append(a)
tr = os.path.join(tmp, 'card', 'ROLAND', 'TR-8S')  # real card layout
kd, sd = os.path.join(tr, 'EXPORT', 'KIT'), os.path.join(tr, 'SAMPLE')
os.makedirs(sd)  # EXPORT\KIT is created by the export
srcs = []
for i in range(11):
    p = os.path.join(sd, f'src{i}.wav')
    app.sf.write(p, rng.uniform(-0.5, 0.5, (4410 * (i + 1), 2)), 48000, subtype='PCM_24')
    srcs.append(p)
digest = lambda: [hashlib.sha256(open(p, 'rb').read()).hexdigest() for p in srcs]
before = digest()
for s in ui.slots:
    s.clear()
ui.assign(srcs)
for s in ui.slots:  # heavy processing on every pad
    s.mono, s.fit, s.norm, s.gain, s.fade_in, s.fade_out, s.hpf, s.start = True, True, True, -3.0, 5, 50, 100.0, 50
ui.slots[3].lo = True
assert app.lib.sd_root(os.path.join(tmp, 'card')) == tr and app.lib.sd_root(tmp) is None
ui.sd_dir.set(os.path.join(tmp, 'card'))  # picking the card itself is enough
ui.name.set('sd test')
assert not ui.save_wavs  # default: only the kit (it carries its own audio)
ui.export_sd()
assert not errors, errors
assert sorted(os.listdir(kd)) == ['sd test.t8k', 'sd test.txt'] and len(os.listdir(sd)) == 11  # just the sources
ui.save_wavs = True
ui.export_sd()  # overwrite question answered yes by the stub
assert not errors, errors
outs = sorted(f for f in os.listdir(sd) if f.startswith('sd test_'))
assert len(outs) == 11 and not any(os.path.isdir(os.path.join(sd, f)) for f in os.listdir(sd))
_, kslots, _ = t8k.read(os.path.join(kd, 'sd test.t8k'))
for i, (sname, x, rate) in enumerate(kslots):
    w, wr = app.sf.read(os.path.join(sd, f'sd test_{t8k.SLOTS[i]}_{sname.lower()}.wav'), dtype='int16', always_2d=True)
    assert wr == rate and np.array_equal(w, x), f'pad {i}: kit and WAV differ'
assert kslots[3][2] == 22050
assert digest() == before, 'original samples were modified'

# a kit without any sample is refused (the TR-8S cannot read it): by the app and by the writer
try:
    t8k.write(os.path.join(tmp, 'none.t8k'), 'none', [(n, t, None) for t, n in t8k.DEFAULT_ROM], kit=meta['kit'])
    raise AssertionError('kit without samples was written')
except ValueError:
    pass
kept = [(s.name, s.audio) for s in ui.slots]
for s in ui.slots:
    s.clear()
assert ui.build() is None and 'at least one sample' in errors[-1][1]
for s, (nm, a) in zip(ui.slots, kept):
    if a is not None:
        s.set(nm, a)

# empty pads play the factory built-in sound: no user sample, no memory, nothing counted
ui.slots[2].clear()
ui.slots[9].clear()
name, out_slots = ui.build()
assert out_slots[2] == ('909 Low Tom', 33, None) and out_slots[9] == ('909 Crash Cymbal', 40, None)
assert len(ui.entries()) == 9
t8k.write(os.path.join(tmp, 'rom.t8k'), name, out_slots, kit=meta['kit'])
_, rs, _ = t8k.read(os.path.join(tmp, 'rom.t8k'))
assert rs[2][1] == 33 and rs[2][0] == '909 LOW TOM' and sum(1 for x in rs if x[2]) == 9  # 9 user samples in the file
# re-using a previous export as a source, then re-exporting under the same names -> refused, file intact
prev = os.path.join(sd, outs[0])
prev_hash = hashlib.sha256(open(prev, 'rb').read()).hexdigest()
ui.assign([prev], 0)
ui.slots[0].name = 'src0'
ui.export_sd()
assert errors and 'original' in errors[-1][1] and digest() == before
assert hashlib.sha256(open(prev, 'rb').read()).hexdigest() == prev_hash

# project round-trip: every parameter and the source audio come back exactly
proj = os.path.join(tmp, 'p.tr8proj')
app.filedialog.asksaveasfilename = lambda **k: proj
ui.slots[5].gain, ui.meta['colors'][5] = -math.inf, 9
ui.name.set('proj kit')
assert ui.save_project() and not ui.dirty
want = [{k: getattr(s, k) for k in app.SLOT_KEYS} for s in ui.slots]
audio = [s.audio for s in ui.slots]
for s in ui.slots:
    s.clear()
ui.open_project(proj)
assert [{k: getattr(s, k) for k in app.SLOT_KEYS} for s in ui.slots] == want
assert all((a is None and s.audio is None) or np.array_equal(a, s.audio) for a, s in zip(audio, ui.slots))
assert ui.name.get() == 'proj kit' and ui.meta['colors'][5] == 9

# library files on the TR-8S: remembered per settings, checked against the live state; loading one again
# applies its TR-8S settings (so it is re-used); changing a setting warns that a 2nd copy would be stored
kick = os.path.join(pack, 'Kicks', 'k1.wav')
up = app.lib.Uploads(os.path.join(tmp, 'up2.json'))
tone = dict(id=640, name='K1', size=4410 * 2, channels=1)
up.add(kick, dict(start=10, fit=True), tone)
state = live(500, 0)
state['tones'] = [dict(tone)]
assert up.on_device(kick, state)[0]['id'] == 640 and up.on_device(kick, None)  # never read: trust what was sent
assert app.lib.Uploads(up.path).on_device(kick, state)  # survives a restart
state['tones'][0]['size'] = 99  # US 17 now holds something else (deleted + re-used slot)
assert not up.on_device(kick, state) and not up.on_device(os.path.join(pack, 'Kicks', 'k2.wav'), state)
ui.uploads = app.lib.Uploads(os.path.join(tmp, 'up3.json'))
ui.mem.state = live(500, 0)
for s_ in ui.slots:
    s_.clear()
ui.assign([kick], 0)
pad = ui.slots[0]
pad.end, pad.fade_out = 3000, 20.0
ui.uploads.add(kick, pad.params(), dict(id=650, name='K1', size=6000, channels=1))
ui.mem.state['tones'] = [dict(id=650, name='K1', size=6000, channels=1)]
assert ui.device_text(pad)[0].startswith('✓ On the TR-8S as US 27')
pad.end = 2500  # a shorter end marker: a different sample for the TR-8S
assert ui.device_text(pad)[1] == app.WARN and 'near-duplicate' in ui.device_text(pad)[0]
assert '2nd copy' in ui.device_text(pad, short=True)[0]
popups = []
app.messagebox.showinfo = lambda *a, **k: popups.append(a[1])
ui.assign([kick], 1)  # loading the file again takes the settings it has on the TR-8S
assert popups and 'Already on the TR-8S' in popups[-1] and 'US 27' in popups[-1] and 'near-duplicate' in popups[-1]
app.messagebox.showinfo = lambda *a, **k: None
assert ui.slots[1].end == 3000 and ui.slots[1].fade_out == 20.0 and ui.device_text(ui.slots[1])[1] == app.ACCENT
ui.browser.fill()
k2 = os.path.join(pack, 'Kicks', 'k2.wav')
assert ui.browser.tree.set(kick, 'dev').startswith('US 27 · ') and ui.browser.tree.set(k2, 'dev') == ''
ui.browser.chips_fill()
ui.browser.pick(app.ON_TR8S)
assert ui.browser.tree.exists(kick) and not ui.browser.tree.exists(os.path.join(pack, 'Kicks', 'k2.wav'))
ui.browser.pick(None)
# library: the TR-8S folder lists every sample on it; a pad can use one as it is (USB only, no new memory)
t27 = f'{app.Browser.TR8S}{ui.mem.state["tones"][0]["id"]}'
assert ui.browser.tree.exists('d:tr8s') and ui.browser.tree.exists(t27)
ui.assign([t27], 9)
assert ui.slots[9].rom == ui.mem.state['tones'][0]['id'] and ui.slots[9].audio is None and 'USB' in ui.pads[9]['texts'][1]
errors = []
app.messagebox.showerror = lambda *a, **k: errors.append(a[1])
assert ui.build() is None and 'Send to TR-8S (USB)' in errors[-1]  # a kit file cannot point at TR-8S samples
app.messagebox.showerror = lambda *a, **k: None
ui.slots[9].clear()
# library: single files can be added (e.g. dropped), and are listed at the top level
ui.browser.add_roots([k2])  # already in a pack: still listed once, in its pack
assert len([it for it in ui.browser.items if it['path'] == k2]) == 1
loose = os.path.join(tmp, 'loose.wav')
app.sf.write(loose, np.full((441, 1), 0.1, 'float32'), 44100)
ui.browser.add_roots([loose, loose])
assert ui.browser.roots.count(loose) == 1 and loose in ui.browser.tree.get_children('')
ui.browser.roots.remove(os.path.normpath(k2))
ui.browser.roots.remove(loose)
ui.browser.rescan()
ui.mem.state = None

# pads and toolbars are drawn on canvases: real clicks on their drawn buttons must reach the right action
root.deiconify()  # drawn only once the window has a real size
root.update()
for p_ in ui.pads:
    p_['drawn'] = None
ui.refresh()
root.update()


def click(cv, tag):
    x0, y0, x1, y1 = cv.bbox(cv.find_withtag(tag)[0])
    cv.event_generate('<Motion>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)
    cv.event_generate('<Button-1>', x=(x0 + x1) // 2, y=(y0 + y1) // 2)
    root.update()


ui.slots[0].set('Click', np.full((441, 1), 0.1, 'float32'))
ui.refresh()
root.update()
click(ui.pads[0]['cv'], 'b:fit')
assert ui.slots[0].fit
click(ui.pads[0]['cv'], 'b:clear')
assert ui.slots[0].audio is None
size = next(i for i, it in enumerate(ui.bar.items) if it['key'] == 1.2)
click(ui.bar, f'b:{size}')
assert ui.zoom == 1.2
ui.set_zoom(1.0)
root.withdraw()

# keys 1..0, - play pads; typing in a text field does not
played = []
ui.play = played.append
ev = lambda c, w: type('E', (), dict(char=c, widget=w))
ui.key(ev('1', root))
ui.key(ev('-', root))
ui.key(ev('2', app.ttk.Entry(root)))
assert played == [0, 10]
root.destroy()
print('OK')
