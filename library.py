"""Sample library (pack scanning, type detection) and the TR-8S memory snapshot. No UI here."""
import json
import os
import re
import time
from collections import Counter

import soundfile as sf

import t8k

EXTS = ('.wav', '.aif', '.aiff', '.flac')
BYTES_PER_SECOND = 44100 * 2
# TR-8S user sample memory (Roland's own figures, confirmed live: free + used blocks = this): every sample takes
# whole 128 KB blocks (1.49 s mono), so even a 0.1 s hat costs 1.49 s.
CAPACITY, BLOCK_BYTES = 0x03300000, 0x20000
DEVICE_SECONDS, DEVICE_SAMPLES, DEVICE_KITS = CAPACITY / BYTES_PER_SECOND, 400, 128


def cost(nbytes):
    """Memory a sample of nbytes really takes on the TR-8S."""
    return -(-nbytes // BLOCK_BYTES) * BLOCK_BYTES


# first match wins, so the more specific types come first; tokens are whole words of the file and folder names
TYPES = [
    ('Open hat', {'open', 'oh', 'ohh', 'ophat', 'openhat'}),
    ('Closed hat', {'closed', 'ch', 'chh', 'hh', 'hat', 'hats', 'hihat', 'hihats', 'closedhat'}),
    ('Kick', {'kick', 'kicks', 'kik', 'bd', 'bassdrum', 'kck'}),
    ('Snare', {'snare', 'snares', 'sd', 'snr'}),
    ('Clap', {'clap', 'claps', 'clp', 'hc', 'handclap'}),
    ('Rim', {'rim', 'rimshot', 'rs', 'stick', 'sidestick'}),
    ('Tom', {'tom', 'toms', 'lt', 'mt', 'ht', 'floortom'}),
    ('Cymbal', {'crash', 'cymbal', 'cymbals', 'cym', 'cc', 'ride', 'rc', 'splash', 'china'}),
    ('Perc', {'perc', 'percs', 'percussion', 'shaker', 'conga', 'bongo', 'cowbell', 'clave', 'tamb', 'tambourine',
              'block', 'cabasa', 'guiro', 'timbale'}),
    ('Stab', {'stab', 'stabs', 'chord', 'chords', 'hit', 'hits'}),
    ('Synth', {'synth', 'synths', 'lead', 'bass', 'pad', 'pluck', 'arp', 'keys', 'organ'}),
    ('FX', {'fx', 'sfx', 'noise', 'riser', 'impact', 'sweep', 'vox', 'vocal', 'vocals', 'foley', 'glitch'}),
]
TYPE_NAMES = [t for t, _ in TYPES] + ['Other']


def categorize(path):
    """Sample type from the file name first, then its folders (closest folder first)."""
    parts = os.path.normpath(os.path.splitext(path)[0]).split(os.sep)
    for part in reversed(parts[-4:]):
        words = set(re.findall(r'[a-z]+', part.lower().replace('hi-hat', 'hihat').replace('open hat', 'openhat')))
        for name, keys in TYPES:
            if words & keys:
                return name
    return 'Other'


def scan(root):
    """Every audio file under root: dicts with path, rel (path inside the pack), type and length in seconds."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs.sort()
        for f in sorted(files):
            if f.lower().endswith(EXTS) and not f.startswith('._'):
                p = os.path.join(d, f)
                try:
                    secs = sf.info(p).duration
                except RuntimeError:
                    continue  # unreadable/corrupt file: leave it out of the library
                out.append(dict(path=p, rel=os.path.relpath(p, root), type=categorize(p), secs=secs))
    return out


SAMPLE_ROOT_MAX = 256  # TR-8S manual: up to 256 loose files directly in ROLAND\TR-8S\SAMPLE


def sd_root(d):
    """The ROLAND\\TR-8S folder for a folder the user picked (the card, ROLAND, or TR-8S itself), or None."""
    for root in (os.path.join(d, 'ROLAND', 'TR-8S'), os.path.join(d, 'TR-8S'), d):
        if os.path.basename(os.path.normpath(root)).upper() == 'TR-8S' and os.path.isdir(root):
            return os.path.normpath(root)
    return None


class Uploads:
    """Which library files are on the TR-8S and with which pad settings: one entry per version sent (the TR-8S
    stores a sample again if any marker, fade or other setting differs). Checked against the live state, so a
    sample deleted on the TR-8S simply stops counting."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding='utf-8') as f:
                self.files = json.load(f)
        except (OSError, ValueError):
            self.files = {}

    @staticmethod
    def key(src):
        return os.path.normcase(os.path.abspath(src))

    def add(self, src, params, tone):
        """Remember that `src` with pad `params` is sample `tone` (a read_tone result) on the TR-8S."""
        versions = [v for v in self.files.get(self.key(src), []) if v['params'] != params and v['id'] != tone['id']]
        versions.append(dict(params=params, id=tone['id'], name=tone['name'], size=tone['size'],
                             channels=tone['channels']))
        self.files[self.key(src)] = versions
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.files, f)
        os.replace(tmp, self.path)

    def on_device(self, src, state):
        """Versions of `src` that are still on the TR-8S (all remembered ones if it was never read)."""
        if not src:
            return []
        tones = {t['id']: t for t in state['tones']} if state else None
        return [v for v in self.files.get(self.key(src), [])
                if tones is None or (v['id'] in tones and all(tones[v['id']][k] == v[k] for k in
                                                              ('name', 'size', 'channels')))]


class Snapshot:
    """The last live read of the TR-8S over USB (tr8s_usb.TR8S.read_all), kept between sessions so the memory
    numbers are there when the TR-8S is not connected. The TR-8S itself is the truth: read it again after changes."""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding='utf-8') as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.state, self.read_at = d.get('state'), d.get('read_at')

    def update(self, state):
        self.state, self.read_at = state, time.time()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dict(state=self.state, read_at=self.read_at), f)
        os.replace(tmp, self.path)

    def usage(self, extra=()):
        """(bytes used, samples, Counter of sample types, kits using user samples) on the TR-8S, plus `extra`
        ledger entries of a kit about to be imported. Importing a kit stores its samples again even when the
        TR-8S has them (hardware tests: two bit-identical kits each re-used only 1 of 11), so they all count."""
        used, n, types, kits = 0, 0, Counter(), 0
        if self.state:
            used = CAPACITY - self.state['storage']['free_bytes']
            n = len(self.state['tones'])
            types = Counter(categorize(t['name']) for t in self.state['tones'])
            kits = sum(1 for k in self.state['kits'] if any(t >= t8k.USER_TONE for t in k['tones']))
        return (used + sum(e['bytes'] for e in extra), n + len(extra), types + Counter(e['type'] for e in extra),
                kits + (1 if extra else 0))
