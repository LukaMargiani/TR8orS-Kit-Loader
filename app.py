"""TR8orS-Kit-Loader: build and edit Roland TR-8S .t8k kits from WAV/AIFF/FLAC samples.
Source sample files are only ever read; all processing is applied on export."""
import base64
import ctypes
import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import zipfile
import tkinter as tk
from collections import Counter
import winsound
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

import numpy as np
import soundfile as sf
from PIL import Image, ImageDraw, ImageTk
from scipy import signal
from tkinterdnd2 import DND_FILES, TkinterDnD

import library as lib
import t8k

APP = 'TR8orS-Kit-Loader'
RATE, LO_RATE = t8k.RATE, 22050
SLOT_MAX, KIT_MAX = 4 << 20, 12 << 20
EXTS = lib.EXTS
KITS_DIR = os.path.join(os.path.expanduser('~'), 'Documents', APP)
SETTINGS = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), APP, 'settings.json')
MEMORY = os.path.join(os.path.dirname(SETTINGS), 'tr8s_live.json')  # last live read of the TR-8S
UPLOADS = os.path.join(os.path.dirname(SETTINGS), 'tr8s_uploads.json')  # library files sent to the TR-8S
# kit parameters for brand-new kit files, copied from the user's own TR-8S (none ship with the app)
KIT_PARAMS = os.path.join(os.path.dirname(SETTINGS), 'kit_params_from_tr8s.bin')
NEED_KIT_PARAMS = ('A kit file needs a starting kit (effects, tune, decay…). Connect your TR-8S by USB and '
                   'press Read TR-8S once (the app copies the settings of a kit from it), or open a .t8k '
                   'exported from your TR-8S.')
PREVIEW = os.path.join(tempfile.gettempdir(), 'tr8ors_preview.wav')
FIT_FADE_MS, DECLICK_MS = 30, 3  # fade where Fit shortens a sound; tiny fade on any edge cut into one
HPF_OFF, LPF_OFF, FADE_MAX = 20.0, 20000.0, 10000.0
PROJ_EXT = '.tr8proj'
KIT_SAMPLES = os.path.join(KITS_DIR, 'Kit samples')  # samples of opened kits, as library files
KEYS = '1234567890-'  # keyboard shortcuts that play pads BD..RC
# pad settings that change the audio sent: any difference means the TR-8S stores another copy
PARAM_KEYS = ('mono', 'lo', 'fit', 'start', 'end', 'fade_in', 'fade_out', 'hpf', 'lpf', 'gain', 'norm')
SLOT_KEYS = ('name', 'rom', 'src', 'type', 'mono', 'lo', 'fit', 'start', 'end', 'fade_in', 'fade_out', 'hpf', 'lpf',
             'gain', 'norm')

# Ableton-style dark gray theme; HEX follows t8k.COLORS (the TR-8S color order)
BG, PANEL, RAISED, HOVER, FIELD, LINE = '#2b2b2b', '#363636', '#454545', '#525252', '#1e1e1e', '#1a1a1a'
TEXT, DIM, ACCENT, WARN, WAVE_BG = '#d6d6d6', '#8f8f8f', '#ffab4a', '#ff5c5c', '#222222'
TOOLBAR = '#1f1f1f'  # darker box for the main actions
USED, UNUSED = '#3fd46b', '#ffe047'  # library: on the TR-8S and used by a kit / on it but in no kit
GHOST = '#4a4a4a'  # the untouched waveform, drawn behind the processed one
SS = 4  # supersampling for smooth (anti-aliased) knobs and graphs
HEX = ['#ff4b4b', '#ff9b3d', '#ffe047', '#b5e34a', '#3fd46b', '#4fd2f5',
       '#8fbfff', '#4a6cff', '#9d5cff', '#f74bd6', '#ff9ac9', '#eeeeee']
# text sizes at 100 %; View size scales them all (named fonts change every widget at once)
FONT_SIZES = dict(base=(10, 'normal'), small=(9, 'normal'), small_bold=(9, 'bold'), bold=(10, 'bold'),
                  text=(11, 'normal'), heading=(12, 'bold'), title=(15, 'normal'), title_bold=(15, 'bold'),
                  mono=(11, 'normal'))
ZOOMS = (0.8, 0.9, 1.0, 1.1, 1.2)
try:  # draw at the screen's real resolution; otherwise Windows stretches (blurs) the window above 100 % scaling
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    DPI = ctypes.windll.user32.GetDpiForSystem() / 96
except (AttributeError, OSError):
    DPI = 1.0


def px(n):
    """Pixels at 100 % Windows scaling -> pixels on this screen (text sizes in points scale by themselves)."""
    return round(n * DPI)


PAD_W, PAD_WAVE_H = px(210), px(60)  # pad width / waveform height at 100 %
F = {}  # the named fonts, made by theme()
# logo and icon: next to app.py, or unpacked by PyInstaller into sys._MEIPASS for the .exe
ASSETS = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__))), 'assets')

# our sample types -> the TR-8S's own sample categories (its browser groups user samples by these)
TR8S_CATEGORY = {k: t8k.CATEGORIES.index(v) for k, v in dict(
    Kick='BD', Snare='SD', Tom='TOM', Rim='RS', Clap='HC', Cymbal='CC/RC', Perc='PERC1', FX='FX/HIT', Synth='SYNTH1',
    Stab='CHORD', **{'Closed hat': 'CH/OH', 'Open hat': 'CH/OH'}).items()}

RULE_OF_THUMB = ('How the TR-8S stores samples: 400 sample slots and 408 memory blocks (1.49 s each). A short hit '
                 '(mono up to 1.48 s, stereo up to 0.74 s) takes 1 block, so with one-shots you run out of slots '
                 'before you run out of memory. Longer or stereo sounds take more blocks.   ? = all the rules')

# (heading, text) for the TR-8S rules window; every number here was measured on the hardware or is Roland's own
RULES = [
    ('Sample memory', 'The TR-8S has one shared memory for all user samples: 408 blocks of 128 KB, 606 seconds in '
     'total (mono, 44.1 kHz). Every kit uses this same pool.'),
    ('Sample slots', 'There are 400 sample slots (US 1 to US 400), also shared by all kits. Each imported sample '
     'takes one slot, however short it is.'),
    ('Blocks: what one sample costs', 'Every sample takes whole blocks, rounded up:\n'
     '• mono up to 1.48 s = 1 block\n• stereo up to 0.74 s = 1 block\n• 22.05 kHz mono up to 2.97 s = 1 block\n'
     '• a 1.6 s stereo sound = 3 blocks\nA 0.1 s hi-hat costs a full block. Each pad shows its blocks.'),
    ('Slots run out first for one-shots', 'With 408 blocks and 400 slots, 400 short one-shots (1 block each) all '
     'fit. You only run out of memory before slots when samples are long or stereo.'),
    ('Making samples smaller', 'Mono halves a stereo sample. Fit (per pad) shortens it to exactly what fits in one '
     'block for its current Mono / 22.05 kHz settings, with a smooth 30 ms fade-out; it follows those settings, so turning '
     'Mono on doubles the length Fit allows. The end marker shortens a sample to any length you choose: whatever '
     'is after it is removed, not silenced, and a fade-out happens inside what is left. 22.05 kHz halves memory again '
     'but removes treble above 11 kHz, so use it for long, dark sounds.'),
    ('No clicks', 'All fades use an S-curve, so they start and end without a corner. Wherever the start marker, end '
     'marker or Fit cuts into a sound, and wherever a sample\'s own end is not on the zero line, a 3 ms fade is '
     'added (unless your own fade is longer), so every sample ends exactly on the zero line. A sample that already '
     'ends on zero and that you leave untouched is sent bit-for-bit, so the TR-8S can recognise it.'),
    ('Kits', 'There are 128 kit slots. A kit is just 11 pads pointing at sounds. Built-in sounds (the 808, 909, '
     '707… voices) use no sample memory at all. An empty pad here plays the built-in 909 sound for that pad.'),
    ('Sending kits over USB (best)', 'Send to TR-8S (USB) puts the kit straight into the kit slot you pick and '
     'saves it, no WRITE needed. Before uploading a pad, the app checks whether an identical sample is already on '
     'the TR-8S (it compares the audio) and re-uses it, so a sample used in many kits is stored once. Only new '
     'samples are uploaded (about 9 s per 1.49 s block). Over USB a kit may use built-in sounds only. A new kit '
     'keeps the kit settings (effects, tune, decay…) of the slot it goes into; an opened kit keeps its own.'),
    ('Importing kits (.t8k from the SD card)', 'Importing a kit always stores all of its samples again, even if '
     'the TR-8S already has identical ones (tested on the hardware). Importing the same kit twice uses the memory '
     'twice. A kit file needs at least one sample: a kit with only built-in sounds gives "Read Error!".'),
    ('After importing: WRITE', 'An imported kit only lives in the edit buffer. Hold [WRITE] and press [KIT], pick '
     'the slot, OK. Without that the kit is lost at power-off, but its samples stay in memory unused.'),
    ('File limits', 'Up to 4 MB per pad and 12 MB per kit file. The TR-8S takes samples up to about 180 s '
     '(mono, 44.1 kHz) each.'),
    ('Unused samples and Optimize', 'Samples no kit uses still take memory. Delete them (TR-8S memory → Delete '
     'unused samples, or UTILITY → SAMPLE:Delete, by number). TR-8S memory also lists every sample: select any and '
     'Delete selected; pads of kits that used a deleted sample switch to the built-in 909 sound for that pad. Deleting leaves gaps; Optimize joins them. Back up '
     'first and never switch off during Optimize.'),
    ('Same sample, other settings = another copy', 'The TR-8S re-uses a sample only if the audio is exactly '
     'the same. The same kick with a different end marker, fade, filter, Volume, Mono, 22.05 kHz or Fit is a different '
     'sample, and sending it stores a near-duplicate. The library marks files already on the TR-8S (TR-8S column, '
     '"On TR-8S" filter). Loading one onto a pad applies the settings it has on the TR-8S automatically; a pad shows '
     '✓ while it matches and ⚠ once you change it ("Use TR-8S settings" in the editor puts it back). Samples sent '
     'before this check existed are not marked until they are sent again.'),
    ('USB connection', 'Connect the TR-8S by USB normally (not Storage mode), stopped, with no other program using '
     'its MIDI ports. Device ID must match (default 17). Reading the TR-8S changes nothing on it.'),
    ('SD card', 'Format the card in the TR-8S (UTILITY → SD CARD FORMAT). Kits go in ROLAND\\TR-8S\\EXPORT\\KIT. '
     'SAMPLE holds up to 256 loose files, or folders of up to 256 files each (no deeper folders). Eject the card in '
     'Windows before unplugging Storage mode.'),
]


# ---- audio: float32 arrays shaped (frames, channels) at 44.1 kHz ----

def resample(a, sr, to=RATE):
    if sr == to:
        return a
    g = math.gcd(sr, to)
    return signal.resample_poly(a, to // g, sr // g, axis=0).astype('float32')


def load_audio(path):
    a, sr = sf.read(path, dtype='float32', always_2d=True)
    if len(a) < 2:
        raise ValueError('file has no audio')
    return resample(a[:, :2], sr)


def fade(a, ms, out):
    """Fade in / out over `ms` with an S-curve (raised cosine): no corner at either end, so no click, and a
    fade-out always lands exactly on zero."""
    n = min(len(a), int(RATE * ms / 1000))
    if n <= 0:
        return a
    r = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, n, dtype='float32')))[:, None]
    a = a.copy()
    if out:
        a[-n:] *= r[::-1]
    else:
        a[:n] *= r
    return a


def filt(a, kind, hz):
    return signal.sosfilt(signal.butter(4, hz, kind, fs=RATE, output='sos'), a, axis=0).astype('float32')


def loud_range(a, db=-60):
    loud = np.nonzero(np.abs(a).max(axis=1) > 10 ** (db / 20))[0]
    if len(loud) < 2:
        raise ValueError('Sample is silent')
    return int(loud[0]), int(loud[-1]) + 1


def play(a, rate):
    winsound.PlaySound(None, 0)  # stop current preview so its file can be overwritten
    if len(a):
        sf.write(PREVIEW, a, rate, subtype='PCM_16')
        winsound.PlaySound(PREVIEW, winsound.SND_FILENAME | winsound.SND_ASYNC)


def safe(name):
    return ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in name).strip() or 'KIT'


class Slot:
    """One pad. `audio` is never modified; everything else is applied on export (non-destructive)."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.name, self.audio, self.rom, self.src, self.type = '', None, None, None, 'Other'
        self.mono = self.lo = self.fit = False
        self.reset()

    def set(self, name, audio, lo=False, src=None):
        self.clear()
        self.name, self.audio, self.lo, self.src = name[:16], audio, lo, src
        self.type = lib.categorize(src or name)
        self.reset()

    def entry(self):
        """What sending this pad adds to the TR-8S (if it is not there yet)."""
        return dict(name=self.name, type=self.type, bytes=lib.cost(self.nbytes()))

    def reset(self):
        """Editor parameters back to neutral."""
        self.start, self.end = 0, 0 if self.audio is None else len(self.audio)
        self.fade_in = self.fade_out = 0.0
        self.hpf, self.lpf = HPF_OFF, LPF_OFF
        self.gain, self.norm = 0.0, False

    def channels(self):
        return 1 if self.mono else self.audio.shape[1]

    def params(self):
        return {k: getattr(self, k) for k in PARAM_KEYS}

    def fit_frames(self, lo=None):
        """Longest length (44.1 kHz frames) that fits in one 128 KB block with this pad's Mono / 22.05 kHz settings:
        1.48 s mono, 0.74 s stereo, twice that at 22.05 kHz."""
        rate = LO_RATE if (self.lo if lo is None else lo) else RATE
        return int(lib.BLOCK_BYTES // (2 * self.channels()) * RATE / rate) - 4  # margin for resampling

    def length(self, lo=None):
        """Frames (at 44.1 kHz) that go to the TR-8S: start to end marker, shortened by Fit if it is on."""
        n = self.end - self.start
        return min(n, self.fit_frames(lo)) if self.fit else n

    def nbytes(self, lo=None):
        """Bytes this pad takes on the TR-8S (before rounding up to whole blocks)."""
        rate = LO_RATE if (self.lo if lo is None else lo) else RATE
        return int(self.length(lo) * rate / RATE) * self.channels() * 2

    def process(self):
        """Float audio as it goes to the TR-8S, before the 22.05 kHz resample and the 16-bit conversion."""
        a = self.audio[self.start:self.end]
        if self.mono:
            a = a.mean(axis=1, keepdims=True)
        if self.hpf > HPF_OFF:
            a = filt(a, 'highpass', self.hpf)
        if self.lpf < LPF_OFF:
            a = filt(a, 'lowpass', self.lpf)
        peak = np.abs(a).max() if self.norm else 0
        a = a * (10 ** (self.gain / 20) / (peak if peak > 0 else 1))  # -inf dB -> silence
        n = self.length()
        if n < len(a):  # Fit: shorten to one block and fade the new end out
            a = fade(a[:n], FIT_FADE_MS, True)
        a = fade(fade(a, self.fade_in, False), self.fade_out, True)
        # de-click: a start marker that cuts into the sound, and any end that is not on the zero line (end marker or
        # the sample's own last frame), get a tiny fade, so playback starts and stops on the axis. A sample that
        # already ends on zero and is otherwise untouched stays bit-identical (needed to recognise it on the TR-8S).
        if self.start > 0 and self.fade_in < DECLICK_MS:
            a = fade(a, DECLICK_MS, False)
        if self.fade_out < DECLICK_MS and (self.end < len(self.audio) or np.abs(a[-1]).max() * 32768 >= 0.5):
            a = fade(a, DECLICK_MS, True)
        return a

    def render(self):
        """(int16 array, sample rate) exactly as it goes to the TR-8S."""
        a = self.process()
        rate = LO_RATE if self.lo else RATE
        # x32768 (not 32767): an untouched 16-bit sample comes back bit-identical, so the TR-8S can match it
        return np.clip((resample(a, RATE, rate) * 32768).round(), -32768, 32767).astype('<i2'), rate


# ---- widgets ----

def theme(root, zoom=1.0):
    root.configure(bg=BG)
    for key, (size, weight) in FONT_SIZES.items():
        family = 'Consolas' if key == 'mono' else 'Segoe UI'
        F[key] = F.get(key) or tkfont.Font(root, family=family, weight=weight)
        F[key].configure(size=round(size * zoom))
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', background=BG, foreground=TEXT, fieldbackground=FIELD, bordercolor=LINE, lightcolor=BG,
                darkcolor=BG, insertcolor=TEXT, font=F['base'])
    s.configure('TEntry', padding=3)
    s.configure('Dim.TLabel', foreground=DIM)
    s.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=px(22 * zoom),
                borderwidth=0)
    s.map('Treeview', background=[('selected', ACCENT)], foreground=[('selected', '#111')])
    s.configure('Treeview.Heading', background=RAISED, foreground=DIM, relief='flat', font=F['small_bold'])
    s.map('Treeview.Heading', background=[('active', HOVER)])
    s.configure('Vertical.TScrollbar', background=RAISED, troughcolor=PANEL, arrowcolor=DIM, bordercolor=PANEL)


def button(parent, text, cmd, **kw):
    return tk.Button(parent, text=text, command=cmd, **{**dict(relief='flat', bd=0, bg=RAISED, fg=TEXT, font=F['base'],
                     activebackground=HOVER, activeforeground=TEXT, padx=7, pady=2, cursor='hand2'), **kw})


class ButtonBar(tk.Canvas):
    """Buttons, texts, images and widgets in a row that wraps when narrow, drawn on one canvas. Windows repaints
    every real widget on each resize step (~1.5 ms each), so groups of buttons are drawn instead of being widgets."""

    def __init__(self, parent, bg, gap=4, font='base'):
        super().__init__(parent, bg=bg, highlightthickness=0, height=1)
        self.bg, self.gap, self.font, self.items, self.lit, self.drawn = bg, gap, font, [], set(), None
        self.bind('<Configure>', lambda e: self.lay())
        self.bind('<Button-1>', self.click)
        self.tag_bind('btn', '<Enter>', lambda e: self.hover(True))
        self.tag_bind('btn', '<Leave>', lambda e: self.hover(False))

    def add(self, text='', cmd=None, key=None, image=None, widget=None, fg=TEXT, font=None, gap=None):
        """A button when cmd is given, else a text (or image / embedded widget). gap: space before it."""
        self.items.append(dict(text=text, cmd=cmd, key=key or text, image=image, widget=widget, fg=fg,
                               font=font, gap=self.gap if gap is None else gap))
        self.drawn = None

    def clear(self):
        self.items, self.drawn = [], None

    def light(self, key, on):
        (self.lit.add if on else self.lit.discard)(key)
        self.drawn = None
        self.lay()

    def lay(self):
        w = self.winfo_width()
        state = (w, tuple(sorted(map(str, self.lit))), F['base'].actual('size'), len(self.items))
        if w < 2 or state == self.drawn:
            return
        self.drawn = state
        self.delete('all')
        bh = F['base'].metrics('linespace') + px(6)
        pos, x, y, row = [], 0, 0, 0
        for it in self.items:
            font = F[it['font'] or self.font]
            iw, ih = ((it['image'].width(), it['image'].height()) if it['image'] else
                      (it['widget'].winfo_reqwidth(), it['widget'].winfo_reqheight()) if it['widget'] else
                      (font.measure(it['text']) + (px(14) if it['cmd'] else 0), bh if it['cmd'] else font.metrics('linespace')))
            if x and x + it['gap'] + iw > w:
                x, y, row = 0, y + row + self.gap, 0
            x += it['gap'] if x else 0
            pos.append((it, x, y, iw, ih, font))
            x, row = x + iw, max(row, ih)
        rows = {}
        for it, x, y, iw, ih, font in pos:  # vertical centre of each wrapped line
            rows[y] = max(rows.get(y, 0), ih)
        for it, x, y, iw, ih, font in pos:
            cy = y + rows[y] / 2
            if it['image']:
                self.create_image(x, cy, image=it['image'], anchor='w')
            elif it['widget']:
                self.create_window(x, cy, window=it['widget'], anchor='w')
            elif it['cmd']:
                on = it['key'] in self.lit
                tag = f'b:{self.items.index(it)}'
                self.create_rectangle(x, cy - ih / 2, x + iw, cy + ih / 2, width=0, fill=ACCENT if on else RAISED,
                                      tags=(tag, 'btn', 'face', 'lit' if on else 'off'))
                self.create_text(x + iw / 2, cy, text=it['text'], fill='#111' if on else TEXT, font=font,
                                 tags=(tag, 'btn'))
            else:
                self.create_text(x, cy, text=it['text'], fill=it['fg'], font=font, anchor='w')
        h = max(rows.values(), default=0) + max(rows, default=0)
        if int(self.cget('height')) != h:
            self.config(height=h)

    def tag(self):
        return next((t for t in self.gettags('current') if t.startswith('b:')), None)

    def click(self, e):
        t = self.tag()
        if t:
            self.items[int(t[2:])]['cmd']()

    def hover(self, on):
        t = self.tag()
        self.config(cursor='hand2' if on else '')
        for item in self.find_withtag(t) if t else ():
            if 'off' in self.gettags(item):
                self.itemconfig(item, fill=HOVER if on else RAISED)


def wrap(label):
    """Let a label's text wrap to its current width instead of being cut off."""
    def fit(e):
        w = max(100, e.width - 20)
        if int(label.cget('wraplength')) != w:
            label.config(wraplength=w)
    label.bind('<Configure>', fit)
    return label


def put(w, **kw):
    """configure() only what changed: Tk redraws a widget on every configure, even to the same value."""
    kw = {k: v for k, v in kw.items() if str(w.cget(k)) != str(v)}
    if kw:
        w.config(**kw)


class Legend(tk.Text):
    """Wrapping text whose color words show their color: {green} and {yellow} as text in that color, {orange} like a
    lit (orange) button. Read-only; as many lines tall as the text needs at the current width."""

    def __init__(self, parent, bg, fg=DIM, font='base', text=''):
        super().__init__(parent, bg=bg, fg=fg, font=F[font], wrap='word', relief='flat', bd=0, highlightthickness=0,
                         height=1, cursor='arrow', takefocus=0, padx=0, pady=0)
        self.tag_configure('green', foreground=USED)
        self.tag_configure('yellow', foreground=UNUSED)
        self.tag_configure('orange', background=ACCENT, foreground='#111')
        self.bind('<Configure>', lambda e: self.fit())
        self.show(text)

    def show(self, text, fg=None):
        self.config(state='normal', **({'fg': fg} if fg else {}))
        self.delete('1.0', 'end')
        for k, part in enumerate(re.split(r'\{(green|yellow|orange)\}', text)):
            self.insert('end', part if k % 2 == 0 else f'\u2009{part}\u2009', () if k % 2 == 0 else (part,))
        self.config(state='disabled')
        self.fit()

    def fit(self):
        lines = self.count('1.0', 'end', 'displaylines') or (1,)
        if int(self.cget('height')) != max(1, lines[0]):
            self.config(height=max(1, lines[0]))


def light(b, on):
    """Orange box while a feature is on."""
    put(b, bg=ACCENT if on else RAISED, fg='#111' if on else TEXT, activebackground=ACCENT if on else HOVER)


def wave_points(a, x0, x1, h):
    """Outline of a's waveform (mono mix, peak per pixel column) between x0 and x1, as one polygon."""
    m = np.clip(a.mean(axis=1), -1, 1)
    cols = max(1, min(int(x1 - x0), len(m)))
    edges = np.linspace(0, len(m), cols + 1).astype(int)[:-1]
    top, bot = np.maximum.reduceat(m, edges), np.minimum.reduceat(m, edges)
    xs, mid = x0 + np.arange(cols) * (x1 - x0) / cols, h / 2
    return np.concatenate([np.column_stack([xs, mid - top * mid]),
                           np.column_stack([xs[::-1], mid - bot[::-1] * mid + 1])]).ravel().tolist()


def draw_wave(cv, a, color, tag='wave'):
    cv.delete(tag)
    w, h = cv.winfo_width(), cv.winfo_height()
    cv.create_line(0, h / 2, w, h / 2, fill=LINE, tags=tag)
    if a is not None and len(a) > 1 and w > 1:
        cv.create_polygon(wave_points(a, 0, w, h), fill=color, outline=color, tags=tag)
    cv.tag_lower(tag)


def magnifier_cursor():
    """A magnifying-glass mouse cursor (Tk has none built in): written once as a Windows .cur file."""
    path = os.path.join(tempfile.gettempdir(), 'tr8ors_zoom.cur')
    if not os.path.exists(path):
        y, x = np.mgrid[:32, :32]
        d = np.hypot(x - 11, y - 11)
        glass = ((d >= 6) & (d <= 9)) | ((abs(x - y) <= 1.5) & (x >= 17) & (x <= 26))
        edge = np.zeros_like(glass)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                edge |= np.roll(np.roll(glass, dy, 0), dx, 1)
        px = np.zeros((32, 32, 4), np.uint8)  # BGRA: black glass, white outline, the rest transparent
        px[edge & ~glass] = 255
        px[glass] = (0, 0, 0, 255)
        mask = np.packbits(px[::-1, :, 3] == 0, axis=1).tobytes()
        img = struct.pack('<IiiHHIIiiII', 40, 32, 64, 1, 32, 0, 0, 0, 0, 0, 0) + px[::-1].tobytes() + mask
        with open(path, 'wb') as f:
            f.write(struct.pack('<HHHBBBBHHII', 0, 2, 1, 32, 32, 0, 0, 11, 11, len(img), 22) + img)
    return '@' + path.replace('\\', '/')


def smooth(cv, width, height, paint):
    """Draw with PIL at SS x size and shrink: Tk canvas lines and arcs are not anti-aliased."""
    im = Image.new('RGB', (width * SS, height * SS), BG)
    paint(ImageDraw.Draw(im))
    cv.photo = ImageTk.PhotoImage(im.resize((width, height), Image.LANCZOS), master=cv)
    cv.create_image(0, 0, image=cv.photo, anchor='nw')


def fmt_time(ms):
    return 'Off' if ms < 1 else f'{ms:.0f} ms' if ms < 1000 else f'{ms / 1000:.2f} s'


def fmt_hz(hz, off):
    return 'Off' if abs(hz - off) < 0.5 else f'{hz:.0f} Hz' if hz < 1000 else f'{hz / 1000:.1f} kHz'


def fmt_db(db):
    return '-inf dB' if db == -math.inf else f'{db:+.1f} dB'


# volume knob: -inf at the bottom, 0 dB at ~81 %, +6 dB at the top
def pos_to_db(p):
    return -math.inf if p < 0.005 else 6 + 66 * math.log10(p)


def db_to_pos(db):
    return 0.0 if db == -math.inf else min(1.0, 10 ** ((db - 6) / 66))


class Knob(tk.Canvas):
    """Drag up/down, mouse wheel or arrow keys to turn; double-click resets."""
    S, H = px(62), px(90)  # dial size, whole height with the texts

    def __init__(self, parent, label, to_val, to_pos, fmt, get, put, default, color, on_change):
        super().__init__(parent, width=self.S, height=self.H, bg=BG, highlightthickness=0, takefocus=1,
                         cursor='sb_v_double_arrow')
        self.bind('<FocusIn>', lambda e: self.draw())  # the focused knob's name turns orange (keys turn it)
        self.bind('<FocusOut>', lambda e: self.draw())
        self.label, self.to_val, self.to_pos, self.fmt = label, to_val, to_pos, fmt
        self.get, self.put, self.default, self.color, self.on_change = get, put, default, color, on_change
        self.bind('<Button-1>', self.press)
        self.bind('<B1-Motion>', lambda e: self.turn(self.p0 + (self.y0 - e.y) / 150))
        self.bind('<Double-Button-1>', lambda e: self.value(self.default))
        self.bind('<MouseWheel>', lambda e: self.turn(self.to_pos(self.get()) + (0.02 if e.delta > 0 else -0.02)))
        for key, d in (('<Up>', .02), ('<Right>', .02), ('<Down>', -.02), ('<Left>', -.02)):
            self.bind(key, lambda e, d=d: self.turn(self.to_pos(self.get()) + d))
        self.draw()

    def press(self, e):
        self.focus_set()
        self.y0, self.p0 = e.y, self.to_pos(self.get())

    def turn(self, p):
        self.value(self.to_val(max(0.0, min(1.0, p))))

    def value(self, v):
        self.put(v)
        self.draw()
        self.on_change()

    def draw(self):
        self.delete('all')
        s, p = self.S, self.to_pos(self.get())
        cx, cy, r = s / 2 * SS, (s / 2 + px(8)) * SS, (s / 2 - px(12)) * SS
        box = (cx - r, cy - r, cx + r, cy + r)

        def paint(d):
            inner = r - px(7) * SS
            d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), fill=PANEL)
            d.arc(box, 135, 405, fill=RAISED, width=px(4) * SS)
            if p > 0.005:
                d.arc(box, 135, 135 + 270 * p, fill=self.color, width=px(4) * SS)
            ang = math.radians(135 + 270 * p)
            d.line((cx, cy, cx + (r - px(2) * SS) * math.cos(ang), cy + (r - px(2) * SS) * math.sin(ang)),
                   fill=TEXT, width=px(2) * SS)
        smooth(self, s, s + px(8), paint)
        self.create_text(s / 2, px(8), text=self.label, fill=ACCENT if self.focus_get() is self else DIM,
                         font=F['small_bold'])
        self.create_text(s / 2, self.H - px(12), text=self.fmt(self.get()), fill=TEXT, font=F['base'])


class App:
    def __init__(self, root):
        cfg = self.load_settings()
        self.root, self.meta, self.dirty = root, {'colors': list(t8k.DEFAULT_COLORS)}, False
        self.slots = [Slot() for _ in t8k.SLOTS]
        self.name = tk.StringVar(value='NEW KIT')
        self.name.trace_add('write', lambda *_: setattr(self, 'dirty', True))
        old = cfg.get('kit_dir')  # settings from before the single SD folder: ...\ROLAND\TR-8S\EXPORT\KIT
        self.sd_dir = tk.StringVar(value=cfg.get('sd_dir') or (os.path.dirname(os.path.dirname(old)) if old else ''))
        self.save_wavs = cfg.get('save_wavs', False)
        self.mem = lib.Snapshot(MEMORY)
        self.uploads = lib.Uploads(UPLOADS)
        self.zoom = cfg.get('zoom', 1.0) if cfg.get('zoom') in ZOOMS else 1.0
        root.title(APP)
        try:  # own taskbar entry with our icon (not Python's) when run as a script
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP)
        except (AttributeError, OSError):
            pass
        root.iconbitmap(default=os.path.join(ASSETS, 'icon.ico'))  # every window of the app
        self.logo_src = Image.open(os.path.join(ASSETS, 'logo.png'))
        root.geometry(cfg.get('geometry', '1560x900'))
        root.minsize(640, 480)
        root.protocol('WM_DELETE_WINDOW', self.quit)
        theme(root, self.zoom)

        # main actions in their own darker box, apart from the library's buttons; wraps when narrow
        box = tk.Frame(root, bg=TOOLBAR, highlightthickness=1, highlightbackground=LINE)
        box.pack(fill='x', padx=10, pady=(10, 6))
        bar = self.bar = ButtonBar(box, TOOLBAR)
        bar.pack(fill='x', padx=8, pady=8)
        bar.add(image=self.logo_image())
        bar.add('TR8orS', fg=ACCENT, font='title_bold', gap=px(8))
        bar.add('KIT LOADER', fg=DIM, font='title', gap=px(4))
        for k, (text, cmd) in enumerate([('New kit', self.new), ('Open…', self.open), ('Load folder…', self.load_folder),
                          ('Save project…', self.save_project), ('Save .t8k…', self.save),
                          ('Send to TR-8S (USB)…', self.send_usb), ('Export to SD card', self.export_sd),
                          ('TR-8S rules', lambda: rules(self.root))]):
            bar.add(text, cmd, gap=px(12) if k == 0 else None)
        bar.add('Kit name', gap=px(14))
        bar.add(widget=ttk.Entry(bar, font=F['base'], textvariable=self.name, width=18))
        bar.add('Size', gap=px(14))
        for z in ZOOMS:
            bar.add(f'{round(z * 100)}%', lambda z=z: self.set_zoom(z), key=z, gap=px(2))

        wrap(tk.Label(root, bg=BG, fg=DIM, font=F['base'], anchor='w', justify='left', text=RULE_OF_THUMB)).pack(
            side='bottom', fill='x', padx=12, pady=(0, 8))
        mem = tk.Frame(root, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        mem.pack(side='bottom', fill='x', padx=10, pady=(0, 4))
        tk.Label(mem, text='TR-8S MEMORY', bg=PANEL, fg=DIM, font=F['small_bold']).pack(side='left', padx=(8, 6))
        self.mem_bar = tk.Canvas(mem, width=180, height=12, bg=FIELD, highlightthickness=0)
        self.mem_bar.pack(side='left', pady=8)
        self.mem_text = Legend(mem, PANEL, TEXT)
        self.mem_text.pack(side='left', padx=8, fill='x', expand=True)
        button(mem, 'Details…', lambda: details(self)).pack(side='right', padx=6, pady=4)
        button(mem, '?', lambda: rules(self.root), padx=8).pack(side='right', padx=(0, 6), pady=4)
        button(mem, 'Read TR-8S (USB)', lambda: read_device(self, self.root)).pack(side='right', pady=4)

        panes = tk.PanedWindow(root, orient='horizontal', bg=LINE, sashwidth=5, bd=0)
        panes.pack(fill='both', expand=True, padx=(10, 0))
        self.browser = Browser(panes, self, cfg.get('roots', []))
        kit = ttk.Frame(panes)
        panes.add(self.browser, minsize=240, width=320)
        panes.add(kit, minsize=360)

        ram = ttk.Frame(kit, padding=(8, 0, 10, 0))
        ram.pack(fill='x')
        self.total = ttk.Label(ram)
        self.total.pack(side='right')

        sd = self.sd_bar = ButtonBar(kit, BG)
        sd.pack(fill='x', padx=(8, 10), pady=(4, 0))
        sd.add('TR-8S SD card')
        sd.add(widget=ttk.Entry(sd, font=F['base'], textvariable=self.sd_dir, width=36))
        sd.add('…', self.browse, gap=px(2))
        sd.add('Also save WAVs to SAMPLE', self.toggle_wavs, key='wavs', gap=px(12))
        sd.light('wavs', self.save_wavs)

        # pads live in a scrolling area: as many columns as fit, more rows when narrow, never squeezed
        scroller = tk.Frame(kit, bg=BG)
        scroller.pack(fill='both', expand=True, padx=(5, 0), pady=(8, 4))
        self.pad_canvas = tk.Canvas(scroller, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(scroller, command=self.pad_canvas.yview)
        self.pad_canvas.config(yscrollcommand=vbar.set)
        vbar.pack(side='right', fill='y')
        self.pad_canvas.pack(side='left', fill='both', expand=True)
        grid = self.grid = tk.Frame(self.pad_canvas, bg=BG)
        win = self.pad_canvas.create_window(0, 0, window=grid, anchor='nw')
        self.pad_canvas.bind('<Configure>', lambda e: (self.pad_canvas.itemconfig(win, width=e.width), self.reflow()))
        grid.bind('<Configure>', lambda e: self.pad_canvas.config(scrollregion=self.pad_canvas.bbox('all')))
        root.bind_all('<MouseWheel>', self.wheel, add='+')
        self.cols = 0
        self.pads = []
        # each pad is drawn on one canvas: Windows moves and repaints every widget on each resize step, and ten
        # widgets per pad made resizing take ~0.4 s per step (one canvas: ~0.05 s)
        for i in range(len(t8k.SLOTS)):
            cv = tk.Canvas(grid, bg=PANEL, highlightthickness=0, cursor='hand2', height=px(250))
            cv.bind('<Configure>', lambda e, i=i: self.draw_pad(i))
            cv.bind('<Button-1>', lambda e, i=i: self.pad_click(i, e))
            cv.tag_bind('btn', '<Enter>', lambda e, cv=cv: self.pad_hover(cv, True))
            cv.tag_bind('btn', '<Leave>', lambda e, cv=cv: self.pad_hover(cv, False))
            cv.drop_target_register(DND_FILES)
            cv.dnd_bind('<<Drop>>', lambda e, i=i: self.drop(e, i))
            self.pads.append(dict(frame=cv, cv=cv, texts=None, drawn=None))
        root.drop_target_register(DND_FILES)
        root.dnd_bind('<<Drop>>', lambda e: self.drop(e, None))
        root.bind_all('<Key>', self.key)
        Legend(kit, BG, text='Click a pad to select + preview (keys 1–0, -) · double-click / drag a library sample '
                             'to load it into the selected pad · click the colored header for the TR-8S color · '
                             '{orange} = on: Mono, 22.05 kHz (half the memory), Fit (shorten to fit 1 memory block, '
                             'with a smooth fade)'
               ).pack(side='bottom', fill='x', padx=8, pady=(0, 6), before=scroller)  # packed first: never squeezed out
        self.sel = 0
        self.light_size()
        self.refresh()
        self.dirty = False

    # ---- layout ----

    def reflow(self):
        """As many pad columns as fit the width (more rows when narrow)."""
        cols = max(1, min(11, self.pad_canvas.winfo_width() // round(PAD_W * self.zoom)))
        if cols == self.cols:
            return
        for c in range(max(cols, self.cols)):
            self.grid.columnconfigure(c, weight=1 if c < cols else 0, uniform='pad' if c < cols else '')
        self.cols = cols
        for i, p in enumerate(self.pads):
            p['frame'].grid(row=i // cols, column=i % cols, sticky='nsew', padx=3, pady=3)

    def wheel(self, e):
        """Mouse wheel scrolls the pads when the pointer is over them (knobs and lists keep their own wheel)."""
        w = self.root.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self.pad_canvas:
            if isinstance(w, (Knob, ttk.Treeview, tk.Toplevel)):
                return
            w = w.master
        if w is self.pad_canvas:
            self.pad_canvas.yview_scroll(-1 if e.delta > 0 else 1, 'units')

    def set_zoom(self, z):
        """View size 80-120 %: all text, list rows, pad width and waveform height follow at once."""
        self.zoom = z
        theme(self.root, z)
        for p in self.pads:
            p['drawn'] = None  # sizes follow the fonts
        self.cols = 0
        self.reflow()
        self.logo_image()
        self.light_size()
        self.refresh()

    def logo_image(self):
        """The record logo for the toolbar, a little taller than the title at this view size, so its colors read on
        the dark toolbar."""
        size = px(46 * self.zoom)
        self.logo_img = ImageTk.PhotoImage(self.logo_src.resize((size, size), Image.LANCZOS), master=self.root)
        if self.bar.items:
            self.bar.items[0]['image'], self.bar.drawn = self.logo_img, None
        return self.logo_img

    def light_size(self):
        for z in ZOOMS:
            self.bar.light(z, z == self.zoom)

    def select(self, i):
        self.sel = i
        for k, p in enumerate(self.pads):
            p['cv'].itemconfig('sel', outline=ACCENT if k == i else LINE, width=px(2) if k == i else 1)

    def key(self, e):
        i = KEYS.find(e.char) if e.char else -1
        if i >= 0 and not isinstance(e.widget, (tk.Entry, ttk.Entry)):
            self.play(i)

    # ---- settings ----

    @staticmethod
    def load_settings():
        try:
            with open(SETTINGS, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def quit(self):
        if not self.confirm_discard():
            return
        try:
            os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
            with open(SETTINGS, 'w', encoding='utf-8') as f:
                json.dump(dict(zoom=self.zoom, geometry=self.root.geometry(), sd_dir=self.sd_dir.get(), save_wavs=self.save_wavs,
                               roots=self.browser.roots), f)
        except OSError:
            pass  # settings are a convenience; never block closing
        self.root.destroy()

    def browse(self):
        d = filedialog.askdirectory(title='Select the TR-8S SD card (or a folder holding a copy of it)')
        if not d:
            return
        root = lib.sd_root(d)
        if not root:
            root = os.path.join(os.path.normpath(d), 'ROLAND', 'TR-8S')
            if not messagebox.askyesno(APP, f'No ROLAND\\TR-8S folder in {d}.\nCards formatted by the TR-8S have one. '
                                            f'Create {root} here (e.g. for a copy of the card on your PC)?'):
                return
            os.makedirs(root, exist_ok=True)
        self.sd_dir.set(root)

    def toggle_wavs(self):
        self.save_wavs = not self.save_wavs
        self.sd_bar.light('wavs', self.save_wavs)

    # ---- pads ----

    PAD_BUTTONS = [[('play', '▶'), ('edit', 'Edit')], [('mono', 'Mono'), ('lo', '22.05 kHz'), ('fit', 'Fit')]]

    def draw_pad(self, i):
        """Draw pad i on its canvas: header, waveform, texts, buttons. Only when something it shows changed."""
        s, p = self.slots[i], self.pads[i]
        cv, w = p['cv'], p['cv'].winfo_width()
        if w < 20 or p['texts'] is None:
            return
        color = HEX[self.meta['colors'][i]]
        key = (w, self.zoom, color, id(s.audio), s.start, s.end, s.mono, s.lo, s.fit, p['texts'])
        if p['drawn'] == key:
            return
        p['drawn'] = key
        name, info, info_fg, dev, dev_fg = p['texts']
        g, line, bold = px(6), F['base'].metrics('linespace'), F['bold'].metrics('linespace')
        cv.delete('all')
        hh = bold + px(6)
        cv.create_rectangle(0, 0, w, hh, fill=color, width=0, tags='head')
        cv.create_text(g, hh / 2, anchor='w', text=f'{KEYS[i]}   {t8k.SLOTS[i]}   ·   {t8k.COLORS[self.meta["colors"][i]]}',
                       fill='#111', font=F['bold'], tags='head')
        cv.create_text(w - g, hh / 2, anchor='e', text='✕', fill='#111', font=F['bold'], tags='b:clear')
        y, wh = hh + g, round(PAD_WAVE_H * self.zoom)
        cv.create_rectangle(g, y, w - g, y + wh, fill=WAVE_BG, width=0)
        cv.create_line(g, y + wh / 2, w - g, y + wh / 2, fill=LINE)
        a = None if s.audio is None else s.audio[s.start:s.end]
        if a is not None and len(a) > 1:
            pts = wave_points(a, g, w - g, wh)
            pts[1::2] = [v + y for v in pts[1::2]]
            cv.create_polygon(pts, fill=color, outline=color)
        y += wh + px(4)
        cv.create_text(g, y, anchor='nw', text=name, fill=TEXT, font=F['bold'])
        y += bold
        t = cv.create_text(g, y, anchor='nw', text=info, fill=info_fg, font=F['base'], width=w - 2 * g)
        y = max(y + 2 * line, cv.bbox(t)[3])  # wrapped text pushes the buttons down instead of running under them
        if dev:
            t = cv.create_text(g, y, anchor='nw', text=dev, fill=dev_fg, font=F['base'], width=w - 2 * g)
            y = cv.bbox(t)[3]
        y += g
        bh = line + px(6)
        for row in self.PAD_BUTTONS:
            bw = (w - 2 * g - px(2) * (len(row) - 1)) / len(row)
            for k, (b, text) in enumerate(row):
                x0 = g + k * (bw + px(2))
                lit = b in ('mono', 'lo', 'fit') and getattr(s, b)
                cv.create_rectangle(x0, y, x0 + bw, y + bh, width=0, fill=ACCENT if lit else RAISED,
                                    tags=(f'b:{b}', 'btn', 'face', 'lit' if lit else 'off'))
                cv.create_text(x0 + bw / 2, y + bh / 2, text=text, fill='#111' if lit else TEXT, font=F['base'],
                               tags=(f'b:{b}', 'btn'))
            y += bh + px(2)
        h = round(y + g)
        sel = i == self.sel
        cv.create_rectangle(0, 0, w - 1, h - 1, outline=ACCENT if sel else LINE, width=px(2) if sel else 1, tags='sel')
        if int(cv.cget('height')) != h:
            cv.config(height=h)

    def pad_click(self, i, e):
        """A click on pad i: its buttons, the header (TR-8S color), the ✕, or anywhere else = select and play."""
        tags = e.widget.gettags('current')
        act = next((t[2:] for t in tags if t.startswith('b:')), None)
        if act == 'clear':
            self.clear(i)
        elif act == 'play':
            self.play(i)
        elif act == 'edit':
            self.edit(i)
        elif act:
            self.toggle(i, act)
        elif 'head' in tags:
            self.pick_color(i, e)
        else:
            self.select(i)
            self.play(i)

    @staticmethod
    def pad_hover(cv, on):
        b = next((t for t in cv.gettags('current') if t.startswith('b:')), None)
        for item in cv.find_withtag(b) if b else ():
            if 'face' in cv.gettags(item) and 'off' in cv.gettags(item):
                cv.itemconfig(item, fill=HOVER if on else RAISED)

    def refresh(self):
        total = ram = 0
        for i, (s, p) in enumerate(zip(self.slots, self.pads)):
            if s.audio is not None:
                b = s.nbytes()
                total += b
                ram += lib.cost(b)
                ch = 'M' if s.mono or s.audio.shape[1] == 1 else 'ST'
                blocks = lib.cost(b) // lib.BLOCK_BYTES
                p['texts'] = (s.name.upper(), f'{s.type} · {s.length() / RATE:.2f}s · {ch}\n{b // 1024} KB · {blocks} '
                              f'block{"s" if blocks > 1 else ""} ({lib.cost(b) / lib.BYTES_PER_SECOND:.1f} s)',
                              WARN if b > SLOT_MAX else DIM, *self.device_text(s, short=True))
            else:
                p['texts'] = (s.name or '— empty —',
                              f'On the TR-8S (US {s.rom - t8k.USER_TONE + 1}): no new memory\nNo preview · send by USB'
                              if s.rom is not None and s.rom >= t8k.USER_TONE else
                              'Built-in sound · no memory\n' if s.rom is not None else
                              f'Plays built-in {t8k.DEFAULT_ROM[i][1]}\nDrop a sample to replace', DIM, '', DIM)
            self.draw_pad(i)
        self.total['text'] = (f'This kit {total / 2 ** 20:.2f} / 12 MB   ·   '
                              f'RAM {ram / lib.BYTES_PER_SECOND:.1f} s (whole 1.49 s blocks)')
        self.total['foreground'] = WARN if total > KIT_MAX else TEXT
        self.select(self.sel)

        used, count, _, kits, add = self.memory()
        cap = lib.DEVICE_SECONDS * lib.BYTES_PER_SECOND
        over = used > cap or count > lib.DEVICE_SAMPLES or kits > lib.DEVICE_KITS
        w = self.mem_bar.winfo_reqwidth()
        self.mem_bar.delete('all')
        self.mem_bar.create_rectangle(0, 0, w * min(1, (used - add) / cap), 12, fill=DIM, width=0)
        self.mem_bar.create_rectangle(w * min(1, (used - add) / cap), 0, w * min(1, used / cap), 12,
                                      fill=WARN if over else ACCENT, width=0)
        read = (time.strftime('read %d %b %H:%M', time.localtime(self.mem.read_at)) if self.mem.state else
                'not read yet: connect it by USB and press Read TR-8S')
        self.mem_text.show(fg=WARN if over else TEXT,
                           text=f'{used / lib.BYTES_PER_SECOND:.1f} / {lib.DEVICE_SECONDS:.0f} s   ·   {count} / '
                                  f'{lib.DEVICE_SAMPLES} samples   ·   {kits} / {lib.DEVICE_KITS} kits   ·   '
                                  f'this kit adds {add / lib.BYTES_PER_SECOND:.1f} s ({{orange}})   ·   {read}')
        self.dirty = True

    def device_status(self, s):
        """(versions of this pad's library file on the TR-8S, the version matching the pad's settings or None)."""
        versions = self.uploads.on_device(s.src, self.mem.state)
        return versions, next((v for v in versions if v['params'] == s.params()), None)

    def device_text(self, s, short=False):
        versions, match = self.device_status(s)
        if match:
            us = match['id'] - t8k.USER_TONE + 1
            return (f'✓ on TR-8S (US {us}): no new memory' if short else
                    f'✓ On the TR-8S as US {us} with exactly these settings: sending re-uses it, no new memory.'), ACCENT
        if versions:
            us = versions[-1]['id'] - t8k.USER_TONE + 1
            return (f'⚠ changed: would store a 2nd copy' if short else
                    f'⚠ This sample is on the TR-8S (US {us}) with other settings: sending it like this stores a '
                    'near-duplicate.'), WARN
        return '', DIM

    def use_device_settings(self, s):
        """Give the pad the settings its library file has on the TR-8S, so sending re-uses that copy."""
        versions = self.device_status(s)[0]
        if versions:
            for k, v in versions[-1]['params'].items():
                setattr(s, k, v)

    def kit_params(self):
        """Kit parameters for a kit file: the opened kit's own, else the copy taken from the user's TR-8S."""
        if self.meta.get('kit'):
            return self.meta['kit']
        try:
            with open(KIT_PARAMS, 'rb') as f:
                return f.read()
        except OSError:
            return None

    def memory(self, extra=None):
        """TR-8S totals if this kit were exported now: (bytes, samples, type Counter, kits, bytes this kit adds)."""
        if extra is None:
            extra = self.entries()
        before = self.mem.usage()[0]
        used, count, types, kits = self.mem.usage(extra)
        return used, count, types, kits, used - before

    def entries(self):
        """What importing this kit adds to the TR-8S: only pads with your samples; built-in sounds use no memory."""
        return [s.entry() for s in self.slots if s.audio is not None]

    def pick_color(self, i, e):
        top = tk.Toplevel(self.root, bg=LINE, padx=4, pady=4)
        top.overrideredirect(True)
        top.geometry(f'+{e.x_root}+{e.y_root}')
        label = tk.Label(top, bg=LINE, fg=TEXT, font=F['base'], text=t8k.COLORS[self.meta['colors'][i]])
        label.grid(row=2, column=0, columnspan=6, sticky='w')
        for k, (cname, hx) in enumerate(zip(t8k.COLORS, HEX)):
            sw = tk.Label(top, bg=hx, width=3, cursor='hand2', bd=2,
                          relief='solid' if k == self.meta['colors'][i] else 'flat')
            sw.grid(row=k // 6, column=k % 6, padx=1, pady=1)
            sw.bind('<Enter>', lambda _, c=cname: label.config(text=c))
            sw.bind('<Button-1>', lambda _, k=k: (self.meta['colors'].__setitem__(i, k), top.destroy(), self.refresh()))
        top.bind('<FocusOut>', lambda _: top.destroy())
        top.bind('<Escape>', lambda _: top.destroy())
        top.focus_force()

    def assign(self, paths, start=None):
        if not paths:
            return messagebox.showinfo(APP, 'No WAV, AIFF or FLAC files found.')
        free = range(start, 11) if start is not None else \
            [i for i, s in enumerate(self.slots) if s.audio is None and s.rom is None]
        errors = [f'{len(paths) - len(free)} file(s) skipped: no free pads'] if len(paths) > len(free) else []
        known = []
        for i, p in zip(free, paths):
            if p.startswith(Browser.TR8S):  # a sample on the TR-8S: the pad plays it as it is (USB only)
                t = next(t for t in self.mem.state['tones'] if t['id'] == int(p[len(Browser.TR8S):]))
                self.slots[i].clear()
                self.slots[i].name, self.slots[i].rom = t['name'], t['id']
                continue
            try:
                self.slots[i].set(os.path.splitext(os.path.basename(p))[0], load_audio(p), src=os.path.abspath(p))
                self.use_device_settings(self.slots[i])  # already on the TR-8S: same settings, so it is re-used
                versions = self.device_status(self.slots[i])[0]
                if versions:
                    known.append(f'  {t8k.SLOTS[i]}: {os.path.basename(p)} (US {versions[-1]["id"] - t8k.USER_TONE + 1})')
            except Exception as ex:
                errors.append(f'{os.path.basename(p)}: {ex}')
        self.refresh()
        if errors:
            messagebox.showwarning(APP, '\n'.join(errors))
        if known:
            messagebox.showinfo(APP, 'Already on the TR-8S:\n' + '\n'.join(known) + '\n\nThese pads got the same '
                                     'settings as their TR-8S copy, so sending re-uses them and costs no memory.\n\n'
                                     'Changing anything on them (start / end marker, fades, filters, Volume, '
                                     'Normalize, Mono, 22.05 kHz, Fit) makes a different sample: the TR-8S would store it '
                                     'again as a near-duplicate and use more memory. Pads show ⚠ when that happens.')

    def drop(self, e, i):
        paths = []
        for p in self.root.tk.splitlist(e.data):
            paths += sorted(os.path.join(p, f) for f in os.listdir(p)) if os.path.isdir(p) else [p]
        self.assign([p for p in paths if p.lower().endswith(EXTS)], i)
        return e.action

    def play(self, i):
        if self.slots[i].audio is not None:
            play(*self.slots[i].render())

    def edit(self, i):
        if self.slots[i].audio is not None:
            Editor(self, i)

    def toggle(self, i, key):
        s = self.slots[i]
        if s.audio is not None:
            setattr(s, key, not getattr(s, key))
            self.refresh()

    def clear(self, i):
        self.slots[i].clear()
        self.refresh()

    # ---- kit files ----

    def confirm_discard(self):
        """True when it is fine to replace the current kit (offers to save the project first)."""
        if not self.dirty or not any(s.audio is not None or s.rom is not None for s in self.slots):
            return True
        answer = messagebox.askyesnocancel(APP, 'Save the current kit as a project first?')
        return answer is False or (answer is True and self.save_project())

    def new(self):
        if not self.confirm_discard():
            return
        for s in self.slots:
            s.clear()
        self.meta = {'colors': list(t8k.DEFAULT_COLORS)}
        self.name.set('NEW KIT')
        self.refresh()
        self.dirty = False

    def load_folder(self):
        d = filedialog.askdirectory(title='Load samples from folder')
        if d:
            self.assign([os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith(EXTS)])

    def open(self):
        if not self.confirm_discard():
            return
        p = filedialog.askopenfilename(initialdir=KITS_DIR, filetypes=[
            ('Projects and kits', f'*{PROJ_EXT} *.t8k'), ('TR8orS project', f'*{PROJ_EXT}'), ('TR-8S kit', '*.t8k')])
        if not p:
            return
        try:
            self.open_project(p) if p.lower().endswith(PROJ_EXT) else self.open_kit(p)
            self.to_library(os.path.splitext(os.path.basename(p))[0])
        except Exception as ex:
            messagebox.showerror(APP, f'Could not open {os.path.basename(p)}:\n{ex}')
        self.refresh()
        self.dirty = False

    def open_kit(self, p):
        name, slots, meta = t8k.read(p)
        self.meta = meta
        self.name.set(name)
        for s, (sname, x, rate) in zip(self.slots, slots):
            if rate is None:
                s.clear()
                s.name, s.rom = sname, x
            else:
                s.set(sname, resample(x.astype('float32') / 32768, rate), lo=rate < RATE)

    def to_library(self, kit):
        """Save the opened kit's samples that have no file of their own as WAVs in Kit samples\\<kit>, and add that
        folder to the library, so they can be used in other kits. Pads then point at those files."""
        pads = [(i, s) for i, s in enumerate(self.slots) if s.audio is not None and not (s.src and os.path.exists(s.src))]
        if not pads:
            return
        d = os.path.join(KIT_SAMPLES, safe(kit))
        os.makedirs(d, exist_ok=True)
        for i, s in pads:
            s.src = os.path.join(d, f'{i + 1:02d} {t8k.SLOTS[i]} {safe(s.name)}.wav')
            sf.write(s.src, s.audio, RATE, subtype='FLOAT')  # float: exactly the pad's audio, nothing lost
        if os.path.normpath(d) in self.browser.roots:
            self.browser.rescan()
        else:
            self.browser.add_roots([d])

    # A project is a zip: project.json (settings, every pad parameter) + the untouched source audio as .npy,
    # so it reopens exactly as left even if the original sample files move or were never files (opened kits).

    def save_project(self):
        os.makedirs(KITS_DIR, exist_ok=True)
        p = filedialog.asksaveasfilename(title='Save project', defaultextension=PROJ_EXT, initialdir=KITS_DIR,
                                         filetypes=[('TR8orS project', f'*{PROJ_EXT}')],
                                         initialfile=f'{safe(self.name.get())}{PROJ_EXT}')
        if not p:
            return False
        b64 = lambda b: base64.b64encode(b).decode() if b else None
        doc = dict(version=2, name=self.name.get(), colors=self.meta['colors'],
                   kit=b64(self.meta.get('kit')), devid=b64(self.meta.get('devid')), base=self.meta.get('base'),
                   slots=[{k: getattr(s, k) for k in SLOT_KEYS} for s in self.slots])
        tmp = p + '.tmp'  # write aside, then swap, so a failed save never destroys the previous project
        try:
            with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
                z.writestr('project.json', json.dumps(doc))
                for i, s in enumerate(self.slots):
                    if s.audio is not None:
                        buf = io.BytesIO()
                        np.save(buf, s.audio)
                        z.writestr(f'pad{i}.npy', buf.getvalue())
            os.replace(tmp, p)
        except OSError as ex:
            messagebox.showerror(APP, f'Could not save project:\n{ex}')
            return False
        self.dirty = False
        return True

    def open_project(self, p):
        with zipfile.ZipFile(p) as z:
            doc = json.loads(z.read('project.json'))
            audio = {i: np.load(io.BytesIO(z.read(f'pad{i}.npy')), allow_pickle=False)
                     for i in range(11) if f'pad{i}.npy' in z.namelist()}
        unb64 = lambda s: base64.b64decode(s) if s else None
        self.meta = {k: v for k, v in dict(colors=doc['colors'], kit=unb64(doc.get('kit')),
                                          devid=unb64(doc.get('devid')), base=doc.get('base')).items() if v is not None}
        self.name.set(doc['name'])
        for i, (s, d) in enumerate(zip(self.slots, doc['slots'])):
            s.clear()
            s.audio = audio.get(i)
            for k in SLOT_KEYS:
                if k in d:
                    setattr(s, k, d[k])
            s.fit = s.fit or d.get('cut', False)  # projects from before Fit had a tail Cut toggle

    def build(self, usb=False):
        """Checks TR-8S limits, offers 22.05 kHz only for pads that must shrink. Returns (kit name, t8k slots) or None.
        usb: the kit goes over USB, not as a file, so it may consist of built-in sounds only."""
        if not usb and any(s.audio is None and s.rom is not None and s.rom >= t8k.USER_TONE for s in self.slots):
            return messagebox.showerror(APP, 'Pads that use a sample from the TR-8S folder of the library can only '
                                             'be sent with Send to TR-8S (USB): a kit file cannot point at samples '
                                             'already on the TR-8S.')
        if not usb and not any(s.audio is not None for s in self.slots):
            # hardware test: a .t8k without audio gives "Read Error!" and leaves the TR-8S unable to write the card
            return messagebox.showerror(APP, 'Add at least one sample: the TR-8S cannot read a kit file that has '
                                             'only built-in sounds. Build that kit on the TR-8S itself.')
        others, n_others, _, k_others = self.mem.usage()
        cap = lib.DEVICE_SECONDS * lib.BYTES_PER_SECOND
        if n_others + len(self.entries()) > lib.DEVICE_SAMPLES or k_others + 1 > lib.DEVICE_KITS:
            return messagebox.showerror(APP, f'The TR-8S would go over {lib.DEVICE_SAMPLES} samples or '
                                             f'{lib.DEVICE_KITS} kits. Free some in TR-8S memory → Details (read it '
                                             'over USB first), or use fewer separate samples.')
        free = cap - others
        used = [s for s in self.slots if s.audio is not None]
        down = [s for s in used if not s.lo and s.nbytes() > SLOT_MAX]
        size = lambda s: s.nbytes(lo=True if s in down else None)
        # the kit file is limited by raw size; the TR-8S memory by whole 128 KB blocks per sample
        fits = lambda: sum(map(size, used)) <= KIT_MAX and sum(lib.cost(size(s)) for s in used) <= free
        # a pad with Fit on is one block already: downsampling it only makes it longer, not smaller
        for s in sorted((s for s in used if not s.lo and not s.fit), key=size, reverse=True):
            if fits():
                break
            if s not in down:
                down.append(s)
        if any(size(s) > SLOT_MAX for s in used) or not fits():
            return messagebox.showerror(APP, f'The kit does not fit (4 MB per pad, 12 MB per kit file, '
                                             f'{free / lib.BYTES_PER_SECOND:.1f} s free on the TR-8S) even at 22.05 kHz.\n'
                                             'Turn on Fit or Mono for long pads, move the end marker, or free memory '
                                             'in TR-8S memory → Details.')
        if down:
            pads = '\n'.join(f'  {t8k.SLOTS[self.slots.index(s)]}: {s.name}' for s in down)
            if not messagebox.askyesno(APP, f'The kit is over the TR-8S memory limit. To fit, these pads must be '
                                            f'downsampled to 22.05 kHz (half the memory, no treble above 11 kHz):\n\n'
                                            f'{pads}\n\nDownsample them and continue?'):
                return None
            for s in down:
                s.lo = True
            self.refresh()
        slots = []
        for s in self.slots:
            if s.audio is not None:
                slots.append((s.name, *s.render()))
            elif s.rom is not None:
                slots.append((s.name, s.rom, None))
            else:  # the TR-8S has no silent tone; an empty pad plays the factory built-in sound (no memory used)
                rom_id, rom_name = t8k.DEFAULT_ROM[len(slots)]
                slots.append((rom_name, rom_id, None))
        return (self.name.get().strip() or 'NEW KIT')[:16], slots

    def save(self):
        params = self.kit_params()
        if not params:
            return messagebox.showinfo(APP, NEED_KIT_PARAMS)
        kit = self.build()
        if not kit:
            return
        os.makedirs(KITS_DIR, exist_ok=True)
        p = filedialog.asksaveasfilename(defaultextension='.t8k', filetypes=[('TR-8S kit', '*.t8k')],
                                         initialdir=KITS_DIR, initialfile=f'{safe(kit[0])}.t8k')
        if not p:
            return
        try:
            t8k.write(p, *kit, **{**self.meta, 'kit': params})
        except OSError as ex:
            return messagebox.showerror(APP, f'Could not save kit:\n{ex}')
        messagebox.showinfo(APP, f'Saved {os.path.basename(p)} (+ .txt).')

    def send_usb(self):
        """Put this kit straight into a kit slot over USB. Samples already on the TR-8S (identical audio) are
        re-used instead of stored again; only new ones are uploaded. Saved on the TR-8S, no WRITE needed."""
        state = read_device(self, self.root)
        if not state:
            return
        have = {t['id'] for t in state['tones']}
        gone = [t8k.SLOTS[i] for i, s in enumerate(self.slots) if s.audio is None and s.rom is not None
                and s.rom >= t8k.USER_TONE and s.rom not in have]
        if gone:
            return messagebox.showerror(APP, f'These pads use a sample that is no longer on the TR-8S: '
                                             f'{", ".join(gone)}. Load another sample into them.')
        name = (self.name.get().strip() or 'NEW KIT')[:16]
        slot = pick_slot(self.root, state, name)
        if slot is None:
            return
        kit = self.build(usb=True)
        if not kit:
            return
        name, pads = kit
        done = dict(new=0, reused=0)

        def status(text):
            self.mem_text.show(text, fg=ACCENT)
            self.root.update()

        def work(dev):
            tones = list(state['tones'])
            ids = []
            with dev.locked(f'TR8orS: sending {name}'):
                for i, (sname, x, rate) in enumerate(pads):
                    if rate is None:  # built-in sound
                        ids.append(x)
                        continue
                    status(f'{t8k.SLOTS[i]}: is {sname} already on the TR-8S?')
                    t = dev.find_sample(x, rate, tones)
                    if t is None:
                        tone, pcmt, data = t8k.sample_record(sname, x, rate, 0,
                                                             category=TR8S_CATEGORY.get(self.slots[i].type, 0))
                        t = dev.upload_sample(tone, pcmt, data,
                                              lambda pct, i=i: status(f'{t8k.SLOTS[i]}: uploading {sname}… {pct}%'))
                        tones.append(dev.read_tone(t))  # a second pad with the same sample re-uses this one
                        done['new'] += 1
                    else:
                        done['reused'] += 1
                    ids.append(t)
                    if self.slots[i].src:
                        self.uploads.add(self.slots[i].src, self.slots[i].params(), next(x for x in tones if x['id'] == t))
                status(f'Writing {name} into kit slot {slot + 1}…')
                params = self.meta.get('kit') or dev.get_data('kit', slot)  # new kit: keep that slot's settings
                dev.write_kit(slot, t8k.kit_body(name, ids, self.meta['colors'], params))
        if on_device(self, self.root, work, 'Sending the kit') is None:
            read_device(self, self.root)  # show what did reach the TR-8S
            return
        unused = unused_tones(self.mem.state)
        messagebox.showinfo(APP, f'{name} is in kit slot {slot + 1} and saved on the TR-8S.\n\n'
                                 f'{done["new"]} samples uploaded, {done["reused"]} already on the TR-8S and re-used, '
                                 f'{sum(1 for p in pads if p[2] is None)} built-in sounds.' +
                                 (f'\n\n{len(unused)} samples on the TR-8S are no longer used by any kit '
                                  '(TR-8S memory → Details → Delete unused samples).' if unused else ''))

    def export_sd(self):
        d = self.sd_dir.get().strip()
        params = self.kit_params()
        if not params:
            return messagebox.showinfo(APP, NEED_KIT_PARAMS)
        root = lib.sd_root(d) if d else None
        if not root:
            return messagebox.showerror(APP, 'Pick the TR-8S SD card first (… next to "TR-8S SD card").' +
                                        (f'\nNo ROLAND\\TR-8S folder found in {d} (is the card inserted?)' if d else ''))
        kit = self.build()
        if not kit:
            return
        # kit straight into EXPORT\KIT; optional WAV copies straight into SAMPLE, kit-name prefix avoids clashes.
        # The .t8k already carries its audio: the WAVs are only for SAMPLE:Import (which would store them again).
        kit_dir, smp_dir = os.path.join(root, 'EXPORT', 'KIT'), os.path.join(root, 'SAMPLE')
        fname = safe(kit[0])
        kit_path = os.path.join(kit_dir, f'{fname}.t8k')
        wavs = [(os.path.join(smp_dir, f'{fname}_{t8k.SLOTS[i]}_{safe(sname)}.wav'), x, rate)
                for i, (sname, x, rate) in enumerate(kit[1]) if self.slots[i].audio is not None and self.save_wavs]
        key = lambda p: os.path.normcase(os.path.abspath(p))
        srcs = {key(s.src) for s in self.slots if s.src}
        clash = [w for w, _, _ in wavs if key(w) in srcs]
        if clash:
            return messagebox.showerror(APP, 'Export would overwrite your original sample(s):\n' + '\n'.join(clash) +
                                        '\n\nRename the kit or pick another sample folder.')
        existing = [p for p in [kit_path] + [w for w, _, _ in wavs] if os.path.exists(p)]
        if existing and not messagebox.askyesno(APP, 'Overwrite these files from an earlier export?\n' +
                                                '\n'.join(os.path.basename(p) for p in existing)):
            return
        try:
            os.makedirs(kit_dir, exist_ok=True)
            t8k.write(kit_path, *kit, **{**self.meta, 'kit': params})
            if wavs:
                os.makedirs(smp_dir, exist_ok=True)
            for w, x, rate in wavs:
                sf.write(w, x, rate, subtype='PCM_16')
        except OSError as ex:
            return messagebox.showerror(APP, f'Could not export:\n{ex}')
        self.refresh()
        loose = len([f for f in os.listdir(smp_dir) if f.lower().endswith(EXTS)]) if wavs else 0
        warn = (f'\n\nNote: SAMPLE now holds {loose} loose files; the TR-8S only lists the first '
                f'{lib.SAMPLE_ROOT_MAX}. Move older ones into a folder.' if loose > lib.SAMPLE_ROOT_MAX else '')
        copies = (f'\nand {len(wavs)} WAV copies (processed identically) to\n  {smp_dir}\n'
                  'Do not also SAMPLE:Import them for this kit: that stores them twice.' if wavs else '')
        messagebox.showinfo(APP, f'Exported {fname}.t8k (+ .txt) to\n  {kit_dir}{copies}\n\nOn the TR-8S:\n'
                                 '1. UTILITY → Import → Kit → EXPORT → pick the kit → pick the destination slot → OK\n'
                                 '2. Save it: hold [WRITE] and press [KIT] → pick the same slot → OK\n'
                                 'Without step 2 the kit is lost at power-off, but its samples stay in memory.\n'
                                 '3. Back here, press Read TR-8S (USB) to update the memory numbers.'
                                 f'{warn}')


class Editor(tk.Toplevel):
    """Simpler-style editor: drag the S/E markers, turn the knobs. Nothing is destructive.
    The waveform shows the result (fades, filters, Volume) over the untouched sample in gray. Dragging up/down on the
    time ruler above it zooms around the clicked point; dragging sideways scrolls."""
    GRAPH_W = px(210)  # filter graph width
    FREQS = np.geomspace(20, 20000, 200)

    def __init__(self, app, i):
        super().__init__(app.root, bg=BG)
        self.app, self.s, self.color, self.drag = app, app.slots[i], HEX[app.meta['colors'][i]], None
        self.v0, self.v1 = 0.0, float(len(self.s.audio))  # visible frames (zoom)
        self.proc = self.pending = self.later = None
        self.title(f'Edit {t8k.SLOTS[i]} — {self.s.name}')
        self.geometry(f'{px(1120)}x{px(500)}')
        self.minsize(px(760), px(400))
        tk.Frame(self, bg=self.color, height=4).pack(fill='x')

        top = ttk.Frame(self, padding=8)
        top.pack(fill='x')
        ttk.Label(top, text='Sample name').pack(side='left')
        self.nm = tk.StringVar(value=self.s.name)
        ttk.Entry(top, font=F['base'], textvariable=self.nm, width=18).pack(side='left', padx=4)
        self.nm.trace_add('write', lambda *_: self.rename())
        self.info = ttk.Label(top)
        self.info.pack(side='right')
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill='x', padx=8, pady=(0, 6))
        self.dev = wrap(tk.Label(bar, bg=BG, fg=DIM, font=F['base'], anchor='w', justify='left'))
        self.dev.pack(side='left', fill='x', expand=True)
        self.dev_btn = button(bar, 'Use TR-8S settings', self.use_device)

        try:
            self.ruler = tk.Canvas(self, height=px(22), bg=TOOLBAR, highlightthickness=0, cursor=magnifier_cursor())
        except tk.TclError:
            self.ruler = tk.Canvas(self, height=px(22), bg=TOOLBAR, highlightthickness=0, cursor='sizing')
        self.ruler.pack(fill='x', padx=8)
        self.ruler.bind('<Button-1>', self.zoom_press)
        self.ruler.bind('<B1-Motion>', self.zoom_drag)
        self.ruler.bind('<Double-Button-1>', lambda e: self.zoom_to(0, len(self.s.audio)))
        self.cv = tk.Canvas(self, height=150, bg=WAVE_BG, highlightthickness=0, cursor='sb_h_double_arrow')
        self.cv.pack(fill='both', expand=True, padx=8)
        self.cv.bind('<Configure>', lambda e: self.schedule())
        self.cv.bind('<Button-1>', self.press)
        self.cv.bind('<B1-Motion>', self.motion)

        ctl = tk.Frame(self, bg=BG)
        ctl.pack(fill='x', padx=8, pady=8)
        fade_knob = dict(to_val=lambda p: FADE_MAX * p ** 3, to_pos=lambda v: (v / FADE_MAX) ** (1 / 3), fmt=fmt_time)
        hz_knob = dict(to_val=lambda p: HPF_OFF * 1000 ** p, to_pos=lambda v: math.log(v / HPF_OFF, 1000))
        self.knobs = []
        for label, attr, kw, default in [('VOLUME', 'gain', dict(to_val=pos_to_db, to_pos=db_to_pos, fmt=fmt_db), 0.0),
                                         ('FADE IN', 'fade_in', fade_knob, 0.0), ('FADE OUT', 'fade_out', fade_knob, 0.0),
                                         ('HPF', 'hpf', dict(hz_knob, fmt=lambda v: fmt_hz(v, HPF_OFF)), HPF_OFF),
                                         ('LPF', 'lpf', dict(hz_knob, fmt=lambda v: fmt_hz(v, LPF_OFF)), LPF_OFF)]:
            k = Knob(ctl, label, get=lambda a=attr: getattr(self.s, a), put=lambda v, a=attr: setattr(self.s, a, v),
                     default=default, color=self.color, on_change=self.changed, **kw)
            k.pack(side='left', padx=(0, 14))
            self.knobs.append(k)
        self.graph = tk.Canvas(ctl, width=self.GRAPH_W, height=Knob.H, bg=BG, highlightthickness=0)
        self.graph.pack(side='left')
        # the sample's own spectrum (gray), so the graph shows what the filters take away
        m = self.s.audio[:RATE * 10].mean(axis=1)
        f, psd = signal.welch(m, RATE, nperseg=min(4096, len(m)))
        db = 10 * np.log10(np.interp(self.FREQS, f, psd) + 1e-12)
        self.spectrum = db - db.max()
        for text, cmd in [('Reset', self.reset), ('Trim silence', self.trim), ('▶ Play', self.play)]:
            button(ctl, text, cmd, padx=12, pady=4).pack(side='right', padx=(6, 0))
        self.norm = button(ctl, 'Normalize', self.toggle_norm, padx=12, pady=4)
        self.norm.pack(side='right', padx=(6, 0))
        light(self.norm, self.s.norm)
        wrap(ttk.Label(self, padding=(10, 0, 10, 8), style='Dim.TLabel',
                  text='Drag on the waveform to move the nearest marker (S = start, E = end). Drag up/down on the '
                       'time ruler to zoom, sideways to scroll, double-click it to see all. Knobs: drag up/down, '
                       'wheel or arrow keys · double-click resets. Normalize = peak to 0 dB, before Volume.')
             ).pack(fill='x')
        self.update_device()
        self.grab_set()

    # ---- view: frames <-> pixels through the zoom window ----

    def f2x(self, f):
        return (f - self.v0) / (self.v1 - self.v0) * self.cv.winfo_width()

    def x2f(self, x):
        return self.v0 + max(0.0, min(1.0, x / max(1, self.cv.winfo_width()))) * (self.v1 - self.v0)

    def zoom_press(self, e):
        self.z0 = e.x, e.y, self.x2f(e.x), self.v1 - self.v0

    def zoom_drag(self, e):
        """Like Simpler: drag down = zoom in around the clicked point, up = out; sideways scrolls."""
        x0, y0, anchor, span0 = self.z0
        span = min(len(self.s.audio), max(64.0, span0 * 2 ** ((y0 - e.y) / 50)))
        v0 = anchor - e.x / max(1, self.cv.winfo_width()) * span
        self.zoom_to(v0, v0 + span)

    def zoom_to(self, v0, v1):
        n, span = len(self.s.audio), v1 - v0
        self.v0 = max(0.0, min(n - span, v0))
        self.v1 = self.v0 + span
        self.schedule()

    # ---- markers ----

    def press(self, e):
        self.drag = 'start' if abs(e.x - self.f2x(self.s.start)) <= abs(e.x - self.f2x(self.s.end)) else 'end'
        self.motion(e)

    def motion(self, e):
        f = int(self.x2f(e.x))
        if self.drag == 'start':
            self.s.start = min(f, self.s.end - 2)
        else:
            self.s.end = max(f, self.s.start + 2)
        self.changed()

    # ---- drawing: bursts of mouse events become one redraw ----

    def schedule(self):
        if not self.pending:
            self.pending = self.after_idle(self.redraw)

    def redraw(self):
        self.pending = None
        if not self.winfo_exists():
            return
        cv, s = self.cv, self.s
        cv.delete('all')
        w, h = cv.winfo_width(), cv.winfo_height()
        cv.create_line(0, h / 2, w, h / 2, fill=LINE)
        i0, i1 = int(self.v0), min(len(s.audio), int(math.ceil(self.v1)) + 1)
        if i1 - i0 > 1:
            cv.create_polygon(wave_points(s.audio[i0:i1], self.f2x(i0), self.f2x(i1), h), fill=GHOST, outline=GHOST)
        if self.proc is None:
            self.proc = s.process()
        a0, a1 = max(i0, s.start), min(i1, s.start + len(self.proc))  # the result, where it is in view
        if a1 - a0 > 1:
            cv.create_polygon(wave_points(self.proc[a0 - s.start:a1 - s.start], self.f2x(a0), self.f2x(a1), h),
                              fill=self.color, outline=self.color)
        xs, xe = self.f2x(s.start), self.f2x(s.end)
        for x0, x1 in ((0, xs), (xe, w)):
            if x1 > x0:
                cv.create_rectangle(max(0, x0), 0, min(w, x1), h, fill=LINE, stipple='gray50', outline='')
        end = s.start + s.length()
        fi, fo = int(s.fade_in * RATE / 1000), int(s.fade_out * RATE / 1000)
        cv.create_line(xs, h, self.f2x(min(s.start + fi, end)), 2, self.f2x(max(end - fo, s.start)), 2,
                       self.f2x(end), h, fill=TEXT, width=1)
        if end < s.end:
            cv.create_line(self.f2x(end), 0, self.f2x(end), h, fill=ACCENT, dash=(4, 3))
            cv.create_text(self.f2x(end) + 4, h - 10, text='FIT (1 block)', anchor='w', fill=ACCENT, font=F['base'])
        for x, t, side in ((xs, 'S', 1), (xe, 'E', -1)):
            cv.create_line(x, 0, x, h, fill=ACCENT, width=2)
            cv.create_polygon(x, 0, x + 14 * side, 0, x + 14 * side, 14, x, 20, fill=ACCENT, outline='')
            cv.create_text(x + 7 * side, 7, text=t, fill='#111', font=F['small_bold'])
        b = s.nbytes()
        self.info['text'] = (f'{s.length() / RATE:.3f}s of {len(s.audio) / RATE:.3f}s  ·  {b // 1024} KB  ·  '
                             f'{lib.cost(b) // lib.BLOCK_BYTES} block(s) on the TR-8S')
        self.draw_ruler()
        self.draw_filter()

    def draw_ruler(self):
        r, w = self.ruler, self.cv.winfo_width()
        r.delete('all')
        span_ms = (self.v1 - self.v0) / RATE * 1000
        step = next((t for t in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000)
                     if t / span_ms * w >= 70), 60000)
        t = math.ceil(self.v0 / RATE * 1000 / step) * step
        while t <= self.v1 / RATE * 1000:
            x = self.f2x(t * RATE / 1000)
            r.create_line(x, 14, x, 22, fill=DIM)
            r.create_text(x + 3, 9, text=fmt_time(t) if t else '0', anchor='w', fill=DIM, font=F['small'])
            t += step
        zoomed = self.v1 - self.v0 < len(self.s.audio) - 1
        hint = r.create_text(w - 6, px(11), anchor='e', fill=ACCENT if zoomed else DIM, font=F['small'],
                             text=f'zoom {len(self.s.audio) / (self.v1 - self.v0):.0f}x · double-click: all' if zoomed
                             else 'drag up/down here to zoom')
        r.tag_lower(r.create_rectangle(r.bbox(hint)[0] - 10, 0, w, px(22), fill=TOOLBAR, outline=''), hint)

    def draw_filter(self):
        """HPF/LPF response (white) over the sample's spectrum: gray = before, color = after the filters."""
        g, s, W, H = self.graph, self.s, self.GRAPH_W, Knob.H
        g.delete('all')
        resp = np.zeros_like(self.FREQS)
        for on, kind, hz in ((s.hpf > HPF_OFF, 'highpass', s.hpf), (s.lpf < LPF_OFF, 'lowpass', s.lpf)):
            if on:
                _, hr = signal.sosfreqz(signal.butter(4, hz, kind, fs=RATE, output='sos'), self.FREQS, fs=RATE)
                resp += 20 * np.log10(np.abs(hr) + 1e-9)
        top, bot = px(18), H - px(16)
        xs = np.linspace(0, W, len(self.FREQS)) * SS

        def ys(db):
            return (top + (3 - np.clip(db, -60, 3)) / 63 * (bot - top)) * SS

        def paint(d):
            d.rectangle((0, top * SS, W * SS, bot * SS), fill=WAVE_BG)
            for hz in (100, 1000, 10000):
                x = math.log(hz / 20, 1000) * W * SS
                d.line((x, top * SS, x, bot * SS), fill=LINE, width=SS)
            for db, color in ((self.spectrum, GHOST), (self.spectrum + resp, self.color)):
                d.polygon(list(zip(xs, ys(db))) + [(W * SS, bot * SS), (0, bot * SS)], fill=color)
            d.line(list(zip(xs, ys(resp))), fill=TEXT, width=2 * SS, joint='curve')
        smooth(g, W, H, paint)
        g.create_text(W / 2, px(8), text='FILTER', fill=DIM, font=F['small_bold'])
        for hz, label in ((100, '100'), (1000, '1k'), (10000, '10k')):
            g.create_text(math.log(hz / 20, 1000) * W, H - px(7), text=label, fill=DIM, font=F['small'])

    def changed(self):
        """Redraw now; the main window and the TR-8S check follow once the mouse rests (they are slower)."""
        self.proc = None
        self.schedule()
        if self.later:
            self.after_cancel(self.later)
        self.later = self.after(150, self.settle)

    def settle(self):
        self.later = None
        self.app.refresh()
        self.update_device()

    def update_device(self):
        """Is this pad's sample on the TR-8S with exactly these settings? (else sending stores another copy)"""
        text, fg = self.app.device_text(self.s)
        self.dev.config(text=text, fg=fg)
        if fg == WARN:
            self.dev_btn.pack(side='right')
        else:
            self.dev_btn.pack_forget()

    def use_device(self):
        self.app.use_device_settings(self.s)
        for k in self.knobs:
            k.draw()
        light(self.norm, self.s.norm)
        self.changed()

    def reset(self):
        self.s.reset()
        for k in self.knobs:
            k.draw()
        light(self.norm, False)
        self.changed()

    def toggle_norm(self):
        self.s.norm = not self.s.norm
        light(self.norm, self.s.norm)
        self.changed()

    def trim(self):
        try:
            self.s.start, self.s.end = loud_range(self.s.audio)
        except ValueError as ex:
            return messagebox.showinfo(APP, str(ex), parent=self)
        self.changed()

    def play(self):
        self.app.play(self.app.slots.index(self.s))

    def rename(self):
        self.s.name = self.nm.get()[:16]
        self.app.refresh()


ON_TR8S = 'On TR-8S'  # library filter: files already on the TR-8S


class Browser(tk.Frame):
    """Sample packs: folder tree, type chips with counts, search. Click previews; double-click / Enter loads into
    the selected pad (several selected samples fill the following pads); or drag samples onto any pad.
    Drop folders or files here to add them. The TR-8S folder lists the samples on the TR-8S (from the last read);
    loading one makes the pad use it as it is, with no new memory (USB only)."""
    TR8S = 'tr8s:'  # tree id prefix of a sample on the TR-8S, e.g. 'tr8s:640'

    def __init__(self, parent, app, roots):
        super().__init__(parent, bg=BG)
        self.app, self.roots, self.items, self.type, self.press_at = app, [r for r in roots if os.path.exists(r)], [], None, None
        head = tk.Frame(self, bg=BG)
        head.pack(fill='x', pady=(0, 6))
        tk.Label(head, text='LIBRARY', bg=BG, fg=DIM, font=F['small_bold']).pack(side='left')
        button(head, '+ Folder', self.add).pack(side='right')
        button(head, '− Remove', self.remove).pack(side='right', padx=4)
        self.q = tk.StringVar()
        self.q.trace_add('write', lambda *_: self.fill())
        search = tk.Frame(self, bg=BG)
        search.pack(fill='x')
        ttk.Label(search, text='Search').pack(side='left', padx=(0, 6))
        ttk.Entry(search, font=F['base'], textvariable=self.q).pack(side='left', fill='x', expand=True)
        self.chips = ButtonBar(self, BG, gap=px(2), font='small')
        self.chips.pack(fill='x', pady=6)
        Legend(self, BG, font='small',
               text='Click = preview · double-click / Enter = load into the selected pad · drag onto a pad · drop '
                    'folders or files here to add them. TR-8S column: {green} = used by a kit, {yellow} = on the '
                    'TR-8S but in no kit.').pack(side='bottom', fill='x', pady=(4, 8))
        box = tk.Frame(self, bg=BG)
        box.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(box, columns=('type', 'len', 'dev'), selectmode='extended')
        for col, text in (('#0', 'Sample'), ('type', 'Type'), ('len', 'Length'), ('dev', 'TR-8S')):
            self.tree.heading(col, text=text, anchor='w')
            self.tree.column(col, minwidth=10, stretch=False, anchor='w')
        self.tree.bind('<Configure>', self.fit_columns)
        self.tree.tag_configure('used', foreground=USED)
        self.tree.tag_configure('unused', foreground=UNUSED)
        bar = ttk.Scrollbar(box, command=self.tree.yview)
        self.tree.config(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self.preview())
        self.tree.bind('<Double-1>', lambda e: self.load(self.app.sel) if self.files() else None)
        self.tree.bind('<Return>', lambda e: self.load(self.app.sel))
        self.tree.bind('<ButtonPress-1>', lambda e: setattr(self, 'press_at', (e.x_root, e.y_root)))
        self.tree.bind('<ButtonRelease-1>', self.release)
        self.tree.drop_target_register(DND_FILES)
        self.tree.dnd_bind('<<Drop>>', self.drop)
        self.rescan()

    def fit_columns(self, e):
        """Columns never show cut-off text: when the list is narrow, whole columns are hidden (Type first, then
        TR-8S, then Length) and the names get the rest of the width."""
        rest, shown = e.width - px(110), []
        for col, w in (('len', px(56)), ('dev', px(100)), ('type', px(70))):
            if w <= rest:
                shown.append(col)
                rest -= w
                self.tree.column(col, width=w)
        cols = tuple(c for c in ('type', 'len', 'dev') if c in shown)
        if tuple(self.tree['displaycolumns']) != cols:
            self.tree['displaycolumns'] = cols or ('',)
        self.tree.column('#0', width=px(110) + rest)

    def rescan(self):
        self.app.root.config(cursor='watch')
        self.app.root.update_idletasks()
        items = {}  # a file in two roots (a dropped file inside an added folder) is listed once
        for r in self.roots:
            for it in lib.scan(r):
                items.setdefault(it['path'], dict(it, root=r))
        self.items = list(items.values())
        self.app.root.config(cursor='')
        self.chips_fill()
        self.fill()

    def device(self):
        """(TR-8S samples by tone id, Counter of how many kits use each) from the last read; empty if never read."""
        st = self.app.mem.state
        if not st:
            return {}, Counter()
        return {t['id']: t for t in st['tones']}, Counter(t for k in st['kits'] for t in set(k['tones']))

    def chips_fill(self):
        self.chips.clear()
        counts = Counter(it['type'] for it in self.items)
        chips = [(None, f'All {len(self.items)}')] + [(t, f'{t} {counts[t]}') for t in lib.TYPE_NAMES if counts[t]]
        if self.app.mem.state:
            chips.append((ON_TR8S, f'On TR-8S {len(self.app.mem.state["tones"])}'))
        self.chips.lit = {str(self.type)}
        for t, text in chips:
            self.chips.add(text, lambda t=t: self.pick(t), key=str(t))
        self.chips.lay()

    def pick(self, t):
        self.type = t
        self.chips_fill()
        self.fill()

    @staticmethod
    def status(versions, kits):
        """(TR-8S column text, row tag) for the TR-8S copies of a sample."""
        if not versions:
            return '', ()
        n = sum(kits[v['id']] for v in versions)
        us = ', '.join(str(v['id'] - t8k.USER_TONE + 1) for v in versions)
        return (f'US {us} · {n} kit{"s" if n != 1 else ""}', ('used',)) if n else (f'US {us} · no kit', ('unused',))

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        q = self.q.get().strip().lower()
        filtering = bool(q or self.type)
        tones, kits = self.device()
        state = dict(tones=list(tones.values())) if self.app.mem.state else None
        for it in self.items:
            text, tag = self.status(self.app.uploads.on_device(it['path'], state), kits)
            if (self.type == ON_TR8S and not text) or (self.type not in (None, ON_TR8S) and it['type'] != self.type) \
                    or (q and q not in it['rel'].lower()):
                continue
            parent = '' if it['path'] == it['root'] else self.folder(it['root'], os.path.dirname(it['rel']), filtering)
            self.tree.insert(parent, 'end', iid=it['path'], text=os.path.basename(it['path']), tags=tag,
                             values=(it['type'], f'{it["secs"]:.2f}s', text))
        if self.type in (None, ON_TR8S) and tones:  # every sample on the TR-8S, also ones not in the library
            folder = self.tree.insert('', 0, iid='d:tr8s', open=self.type == ON_TR8S or bool(q),
                                      text=f'TR-8S samples (read {time.strftime("%d %b %H:%M", time.localtime(self.app.mem.read_at))})')
            for t in tones.values():
                if not q or q in t['name'].lower():
                    text, tag = self.status([t], kits)
                    self.tree.insert(folder, 'end', iid=f'{self.TR8S}{t["id"]}', text=t['name'], tags=tag,
                                     values=(lib.categorize(t['name']), f'{t["size"] / 2 / RATE:.2f}s', text))

    def folder(self, root, rel, open_all):
        iid = f'd:{root}|{rel}'
        if not self.tree.exists(iid):
            parent = '' if not rel else self.folder(root, os.path.dirname(rel), open_all)
            self.tree.insert(parent, 'end', iid=iid, text=os.path.basename(rel or root), open=open_all or not rel)
        return iid

    def files(self):
        return [i for i in self.tree.selection() if not i.startswith('d:')]

    def preview(self):
        files = self.files()
        if len(files) == 1 and not files[0].startswith(self.TR8S):  # TR-8S audio is not on the PC: no preview
            try:
                a, sr = sf.read(files[0], dtype='float32', always_2d=True)
                play(a[:, :2], sr)
            except RuntimeError:
                pass  # unreadable file: preview is best-effort

    def load(self, pad):
        files = self.files()
        if files:
            self.app.assign(files, pad)
            self.app.select(min(pad + len(files), 10))  # next pad, so a kit can be built click by click

    def release(self, e):
        if not self.press_at or abs(e.x_root - self.press_at[0]) + abs(e.y_root - self.press_at[1]) < 8:
            return
        w = self.app.root.winfo_containing(e.x_root, e.y_root)
        frames = [p['frame'] for p in self.app.pads]
        while w is not None and w not in frames:
            w = w.master
        if w is not None:
            self.load(frames.index(w))

    def add_roots(self, paths):
        """Add sample folders or single sample files to the library (each once)."""
        new = [os.path.normpath(p) for p in paths if os.path.isdir(p) or p.lower().endswith(EXTS)]
        new = [p for p in dict.fromkeys(new) if p not in self.roots]
        if new:
            self.roots += new
            self.rescan()

    def drop(self, e):
        self.add_roots(self.app.root.tk.splitlist(e.data))
        return e.action

    def add(self):
        d = filedialog.askdirectory(title='Add a sample folder or sample pack to the library')
        if d:
            self.add_roots([d])

    def remove(self):
        sel = self.tree.selection()
        root = next((r for r in self.roots if sel and (sel[0] == r or sel[0].startswith(f'd:{r}|') or
                                                      sel[0].startswith(r + os.sep))), None)
        if not root:
            return messagebox.showinfo(APP, 'Select something inside the sample pack you want to remove from the '
                                            'library. Files on disk are not touched.')
        self.roots.remove(root)
        self.rescan()


def rules(parent):
    """The TR-8S rules: everything about its memory, slots and imports in one readable window."""
    top = tk.Toplevel(parent, bg=BG, padx=12, pady=12)
    top.title('TR-8S rules')
    top.geometry(f'{px(620)}x{px(640)}')
    text = tk.Text(top, bg=PANEL, fg=TEXT, wrap='word', relief='flat', padx=14, pady=10, font=F['text'],
                   spacing1=2, spacing3=2, highlightthickness=0)
    bar = ttk.Scrollbar(top, command=text.yview)
    text.config(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    text.pack(fill='both', expand=True)
    text.tag_config('h', foreground=ACCENT, font=F['heading'], spacing1=10)
    for heading, body in RULES:
        text.insert('end', heading + '\n', 'h')
        text.insert('end', body + '\n')
    text.config(state='disabled')  # read-only, but still selectable and scrollable


def on_device(app, parent, work, what):
    """Open the TR-8S over USB, run work(dev), read the whole machine back and keep that as the memory snapshot.
    Problems are reported, never raised. Returns the live state, or None."""
    parent.config(cursor='watch')
    parent.update_idletasks()
    try:
        import tr8s_usb  # imported here so the app still starts without the MIDI library
        dev = tr8s_usb.TR8S()
        try:
            if dev.is_playing():
                messagebox.showerror(APP, 'Stop the TR-8S first.', parent=parent)
                return None
            work(dev)
            state = dev.read_all()
            if not os.path.exists(KIT_PARAMS):  # one-time copy from the user's own TR-8S (an empty "----" kit if any)
                k = next((k['id'] for k in state['kits'] if k['name'].strip('-') == ''), 0)
                os.makedirs(os.path.dirname(KIT_PARAMS), exist_ok=True)
                with open(KIT_PARAMS, 'wb') as f:
                    f.write(dev.get_data('kit', k))
        finally:
            dev.close()
    except Exception as ex:
        messagebox.showerror(APP, f'{what} over USB failed:\n{ex}', parent=parent)
        return None
    finally:
        parent.config(cursor='')
    app.mem.update(state)
    app.refresh()
    app.browser.chips_fill()
    app.browser.fill()
    return state


def read_device(app, parent):
    return on_device(app, parent, lambda dev: None, 'Reading the TR-8S')


def pick_slot(parent, state, name):
    """Ask which of the 128 kit slots to put the kit in; returns 0-127 or None. Suggests the slot that already
    holds a kit with this name (sending again updates it), else the last slot."""
    tones = {t['id'] for t in state['tones']}
    top = tk.Toplevel(parent, bg=BG, padx=12, pady=12)
    top.title('Send to TR-8S: pick a kit slot')
    top.geometry(f'{px(440)}x{px(520)}')
    top.grab_set()
    tk.Label(top, bg=BG, fg=TEXT, font=F['base'], justify='left', wraplength=410,
             text=f'Which kit slot should "{name}" go into? The kit now in that slot is replaced (its samples stay '
                  'on the TR-8S until you delete unused samples).').pack(fill='x', pady=(0, 8))
    box = tk.Listbox(top, bg=PANEL, fg=TEXT, selectbackground=ACCENT, selectforeground='#111', font=F['mono'],
                     highlightthickness=0, activestyle='none')
    for k in state['kits']:
        mine = sum(1 for t in set(k['tones']) if t in tones)
        box.insert('end', f'{k["id"] + 1:3d}  {k["name"]:<16}  ' + (f'{mine} of your samples' if mine else 'built-in'))
    pick = next((k['id'] for k in state['kits'] if k['name'].upper() == name.upper()), 127)
    box.selection_set(pick)
    box.see(pick)
    box.pack(fill='both', expand=True)
    result = []

    def ok():
        sel = box.curselection()
        if sel and messagebox.askyesno(APP, f'Replace kit slot {sel[0] + 1} ({state["kits"][sel[0]]["name"]}) '
                                            f'with "{name}"?', parent=top):
            result.append(sel[0])
            top.destroy()
    row = tk.Frame(top, bg=BG)
    row.pack(fill='x', pady=(8, 0))
    button(row, 'Cancel', top.destroy).pack(side='right')
    button(row, 'Send', ok).pack(side='right', padx=6)
    box.bind('<Double-1>', lambda e: ok())
    top.wait_window()
    return result[0] if result else None


def unused_tones(state):
    in_use = {t for k in state['kits'] for t in k['tones']}
    return [t for t in state['tones'] if t['id'] not in in_use]


def details(app):
    """TR-8S memory window: what is on the machine (read live over USB) and tools to clean it up."""
    top = tk.Toplevel(app.root, bg=BG, padx=12, pady=12)
    top.title('TR-8S memory')
    top.geometry(f'{px(760)}x{px(820)}')
    tk.Label(top, bg=BG, fg=DIM, justify='left', wraplength=670, font=F['base'],
             text=f'The TR-8S keeps every imported sample in one shared memory: {lib.DEVICE_SECONDS:.0f} s (mono, '
                  f'44.1 kHz) and {lib.DEVICE_SAMPLES} samples for all {lib.DEVICE_KITS} kits. Every sample takes '
                  'whole 1.49 s blocks: up to 1.48 s mono (or 0.74 s stereo) is one block. Importing a kit stores '
                  'its samples again even if the TR-8S already has them. Connect the TR-8S by USB (not Storage '
                  'mode) to read it; reading changes nothing.').pack(fill='x')

    usb = tk.Frame(top, bg=PANEL, padx=8, pady=6)
    usb.pack(fill='x', pady=8)
    report = tk.Label(usb, bg=PANEL, fg=TEXT, justify='left', anchor='nw', wraplength=480, font=F['base'])
    report.pack(side='left', fill='both', expand=True)
    acts = tk.Frame(usb, bg=PANEL)
    acts.pack(side='right', anchor='n')

    box = tk.Frame(top, bg=BG)
    box.pack(fill='x')
    tree = ttk.Treeview(box, columns=('n', 'secs'), selectmode='none', height=6)
    for col, text, w in (('#0', 'Kits using your samples', 420), ('n', 'Samples', 80), ('secs', 'Memory', 90)):
        tree.heading(col, text=text, anchor='w')
        tree.column(col, width=w, anchor='w')
    tree.pack(fill='x')

    head = tk.Frame(top, bg=BG)
    head.pack(fill='x', pady=(10, 2))
    tk.Label(head, bg=BG, fg=DIM, font=F['small_bold'], text='SAMPLES ON THE TR-8S  (select one or more)').pack(
        side='left')
    box2 = tk.Frame(top, bg=BG)
    box2.pack(fill='both', expand=True)
    samples = ttk.Treeview(box2, columns=('mem', 'kits'), selectmode='extended')
    for col, text, w in (('#0', 'Sample', 230), ('mem', 'Memory', 110), ('kits', 'Used by', 340)):
        samples.heading(col, text=text, anchor='w')
        samples.column(col, width=w, anchor='w')
    bar = ttk.Scrollbar(box2, command=samples.yview)
    samples.config(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    samples.pack(fill='both', expand=True)
    types = tk.Label(top, bg=BG, fg=TEXT, justify='left', anchor='w', wraplength=670, font=F['base'])
    types.pack(fill='x', pady=(8, 0))

    def status(text):
        report.config(text=text)
        top.update()

    def show():
        st = app.mem.state
        tree.delete(*tree.get_children())
        samples.delete(*samples.get_children())
        for b in (del_btn, opt_btn, sel_btn):
            b.config(state='normal' if st else 'disabled')
        if not st:
            report.config(text='Not read yet. Connect the TR-8S by USB and press Read TR-8S.')
            types.config(text='')
            return
        tones = {t['id']: t for t in st['tones']}
        used, n, counts, kits = app.mem.usage()
        lines = [f'TR-8S firmware {st["version"][0]}, '
                 + time.strftime('read %d %b %H:%M', time.localtime(app.mem.read_at)),
                 f'{used / lib.BYTES_PER_SECOND:.1f} / {lib.DEVICE_SECONDS:.0f} s used · {n} / {lib.DEVICE_SAMPLES} '
                 f'samples · {kits} of 128 kits use your samples']
        sto = st['storage']
        if sto['longest_free'] < sto['free_bytes']:
            lines.append(f'Fragmented: the largest free gap is {sto["longest_free"] / lib.BYTES_PER_SECOND:.1f} s '
                         'of the free memory. Optimize joins the gaps.')
        unused = unused_tones(st)
        if unused:
            secs = sum(lib.cost(t['size'] * t['channels']) for t in unused) / lib.BYTES_PER_SECOND
            lines.append(f'⚠ {len(unused)} sample{"s" if len(unused) > 1 else ""} ({secs:.1f} s) not used by any kit: '
                         + ', '.join(f'US {t["us"]} {t["name"]}' for t in unused))
        report.config(text='\n'.join(lines))
        for k in st['kits']:
            mine = sorted({t for t in k['tones'] if t in tones})
            if mine:
                mem = sum(lib.cost(tones[t]['size'] * tones[t]['channels']) for t in mine) / lib.BYTES_PER_SECOND
                tree.insert('', 'end', text=f'{k["id"] + 1:3d}  {k["name"]}', values=(len(mine), f'{mem:.1f} s'))
        for t in st['tones']:
            users = [f'{k["id"] + 1} {k["name"]}' for k in st['kits'] if t['id'] in k['tones']]
            cost = lib.cost(t['size'] * t['channels'])
            samples.insert('', 'end', iid=str(t['id']), text=f'US {t["us"]:<4} {t["name"]}',
                           values=(f'{cost // lib.BLOCK_BYTES} block{"s" if cost > lib.BLOCK_BYTES else ""} '
                                   f'({cost / lib.BYTES_PER_SECOND:.1f} s)', ', '.join(users) or 'no kit (unused)'))
        types.config(text='Samples on the TR-8S by type: ' +
                          (' · '.join(f'{t} {counts[t]}' for t in lib.TYPE_NAMES if counts[t]) or '—'))

    def read():
        read_device(app, top)
        show()

    def delete_unused():
        unused = unused_tones(app.mem.state)
        if not unused:
            return messagebox.showinfo(APP, 'Every sample on the TR-8S is used by a kit. Read the TR-8S again if '
                                            'you changed it since.', parent=top)
        secs = sum(lib.cost(t['size'] * t['channels']) for t in unused) / lib.BYTES_PER_SECOND
        if not messagebox.askyesno(APP, f'Delete these {len(unused)} samples from the TR-8S? No kit uses them '
                                        f'({secs:.1f} s of memory). This cannot be undone.\n\n' +
                                        ', '.join(f'US {t["us"]} {t["name"]}' for t in unused), parent=top):
            return

        def work(dev):
            kits = [dev.read_kit(k) for k in range(128)]  # fresh: respects a kit changed on the TR-8S since
            with dev.locked('TR8orS: deleting samples'):
                for k, t in enumerate(unused, 1):
                    status(f'Deleting {k} / {len(unused)}: US {t["us"]} {t["name"]}…')
                    dev.delete_tone(t['id'], kits)  # refuses any sample a kit uses
        if on_device(app, top, work, 'Deleting samples') is None:
            read_device(app, top)  # show what did get deleted
        show()

    def delete_selected():
        ids = [int(i) for i in samples.selection()]
        if not ids:
            return messagebox.showinfo(APP, 'Select one or more samples in the list first.', parent=top)
        st = app.mem.state
        chosen = [t for t in st['tones'] if t['id'] in ids]
        affected = []
        for k in st['kits']:
            pads = [f'{t8k.SLOTS[i]} → {t8k.DEFAULT_ROM[i][1]}' for i, t in enumerate(k['tones']) if t in ids]
            if pads:
                affected.append(f'  Kit {k["id"] + 1} "{k["name"]}":  ' + ', '.join(pads))
        secs = sum(lib.cost(t['size'] * t['channels']) for t in chosen) / lib.BYTES_PER_SECOND
        samples_text = '\n'.join(f'  US {t["us"]} {t["name"]}' for t in chosen)
        if affected:
            text = (f'⚠ {"These samples are" if len(chosen) > 1 else "This sample is"} used by '
                    f'{len(affected)} kit{"s" if len(affected) > 1 else ""}. Deleting will switch those pads to the '
                    'default built-in sound:\n\n' + '\n'.join(affected) + '\n\n')
        else:
            text = ''
        text += (f'Delete {len(chosen)} sample{"s" if len(chosen) > 1 else ""} from the TR-8S ({secs:.1f} s of '
                 f'memory)? This cannot be undone.\n\n{samples_text}')
        if not (messagebox.askyesno if not affected else messagebox.askokcancel)(APP, text, parent=top,
                                                                                  icon='warning' if affected else 'question'):
            return

        def work(dev):
            with dev.locked('TR8orS: deleting samples'):
                dev.delete_samples(ids, status)
        if on_device(app, top, work, 'Deleting samples') is None:
            read_device(app, top)  # show what did get deleted
        show()

    def optimize():
        sto = app.mem.state['storage']
        if sto['longest_free'] >= sto['free_bytes']:
            return messagebox.showinfo(APP, 'Nothing to optimize: the free memory is already in one piece. Read the '
                                            'TR-8S again if you changed it since.', parent=top)
        if not messagebox.askyesno(APP, "Optimize joins the free gaps in the sample memory. Roland's advice: make a "
                                        'backup first, and never switch the TR-8S off while it runs (it can take '
                                        'minutes). Start Optimize now?', parent=top):
            return

        def work(dev):
            with dev.locked('TR8orS: optimizing'):
                dev.optimize(lambda pct: status(f'Optimizing… {pct}%  (do not switch the TR-8S off)'))
        on_device(app, top, work, 'Optimize')
        show()

    button(acts, 'Read TR-8S (USB)', read).pack(fill='x', pady=1)
    del_btn = button(acts, 'Delete unused samples…', delete_unused)
    del_btn.pack(fill='x', pady=1)
    opt_btn = button(acts, 'Optimize…', optimize)
    opt_btn.pack(fill='x', pady=1)
    sel_btn = button(head, 'Delete selected…', delete_selected)
    sel_btn.pack(side='right')
    samples.bind('<Delete>', lambda e: delete_selected())
    show()


if __name__ == '__main__':
    root = TkinterDnD.Tk()
    App(root)
    root.mainloop()
