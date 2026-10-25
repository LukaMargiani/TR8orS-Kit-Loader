"""Talk to a Roland TR-8S over USB MIDI (Roland SysEx: RQ1 = request, DT1 = data).

Protocol facts come from the TR-8S code of Roland's former AIRA web app (archived in github.com/compuphonic/TR-8S-SysEx)
and match the kit/backup formats in t8k.py. Reads are free; every change follows Roland's own sequence, with the
panel locked while it runs.
"""
import contextlib
import struct
import time

import rtmidi

import t8k

MODEL = [0x00, 0x00, 0x00, 0x45]
RQ1, DT1 = 0x11, 0x12
DEFAULT_DEVICE_ID = 0x10  # UTILITY > MIDI > Device ID 17
UTILITY = [0x50, 0x00, 0x00, 0x00]
COMMAND = dict(playing=0x10, lock=0x11, display=0x12, version=0x13, optimize=0x20, freeArea=0x21, freeToneCount=0x22,
               freeTone=0x23, deleteTone=0x24)
WRITE = dict(kit=0x02, tone=0x03)  # utility offsets that save a kit / tone slot to memory; tone 16383 = all
SEND = dict(kit=0x50, tone=0x60, pcmTone=0x70, sample=0x72)   # start a data transfer to the TR-8S
GET = dict(kit=0x51, tone=0x61, pcmTone=0x71, sample=0x73)    # start a data transfer from the TR-8S
SIZE = dict(kit=1312, tone=36, pcmTone=64)
PROGRESS = [0x01, 0x10]  # utility offset where the TR-8S reports transfer progress (percent)
SEND_GAP = 0.064        # Roland waits this long between change messages
CHUNK_GAP = 0.05        # ... and this long between data chunks
MAX_ADDRESS = 0x03FFFFFF  # end of user sample memory


def pack7(data):
    """8 bytes per 7: a header holding the 7 top bits (byte j's top bit in header bit j), then the low 7 bits."""
    out = []
    for i in range(0, len(data), 7):
        group = data[i:i + 7]
        out.append(sum((b >> 7 & 1) << j for j, b in enumerate(group)))
        out += [b & 0x7f for b in group]
    return out


def unpack7(data):
    out = bytearray()
    for i in range(0, len(data), 8):
        head = data[i]
        out += bytes(b | (head >> j & 1) << 7 for j, b in enumerate(data[i + 1:i + 8]))
    return bytes(out)
MIN_TONE, MAX_TONE = 624, 1023  # user samples US 1..400
BLOCK = [0x01, 0x00, 0x00]      # one kit / tone per 7-bit address block
# (base address, bytes) of per-kit and per-tone parameters in the temporary area
KIT_NAME, KIT_TONE = ([0x10, 0, 0, 0], 16), ([0x10, 0, 0x10, 0], 16)  # tone ids: 11 entries, 0x80 apart
TONE_NAME, TONE_CATEGORY, TONE_TYPE = ([0x30, 0, 0, 0], 16), ([0x30, 0, 0, 0x10], 1), ([0x30, 0, 0, 0x11], 1)
TONE_ADDRESS, TONE_SIZE, TONE_CHANNEL = ([0x40, 0, 0, 0], 8), ([0x40, 0, 0, 0x10], 8), ([0x40, 0, 0, 0x38], 1)


def enc7(value, n):
    """Big-endian 7 bits per byte."""
    return [(value >> 7 * (n - 1 - i)) & 0x7f for i in range(n)]


def dec7(data):
    v = 0
    for b in data:
        v = v << 7 | b & 0x7f
    return v


def nibbles(data):
    """Big-endian 4 bits per byte."""
    v = 0
    for b in data:
        v = v << 4 | b & 0x0f
    return v


def offset(address, step, count=1):
    return enc7(dec7(address) + dec7(step) * count, 4)


def checksum(body):
    return (128 - sum(body) % 128) % 128


class TR8SError(Exception):
    pass


class TR8S:
    def __init__(self, device_id=DEFAULT_DEVICE_ID, timeout=2.0):
        self.dev, self.timeout = device_id, timeout
        self.inp, self.out = rtmidi.MidiIn(), rtmidi.MidiOut()
        self.inp.ignore_types(sysex=False, timing=True, active_sense=True)
        last = None
        for hint in ('TR-8S CTRL', 'TR-8S'):  # Roland's app talked to the CTRL port; fall back to the main one
            i = next((k for k, n in enumerate(self.inp.get_ports()) if hint in n and ('CTRL' in n) == ('CTRL' in hint)), None)
            o = next((k for k, n in enumerate(self.out.get_ports()) if hint in n and ('CTRL' in n) == ('CTRL' in hint)), None)
            if i is None or o is None:
                continue
            self.inp.open_port(i)
            self.out.open_port(o)
            try:
                self.version = self.read_version()
                self.port = hint
                return
            except TR8SError as ex:
                last = ex
                self.inp.close_port()
                self.out.close_port()
        self.close()
        raise TR8SError(f'No answer from a TR-8S over USB ({last or "no TR-8S MIDI ports"}). Check the cable, that '
                        'the TR-8S is not in Storage mode, that UTILITY > MIDI > Device ID is 17 (the factory setting) and '
                        'that no other program uses it.')

    def close(self):
        self.inp.close_port()
        self.out.close_port()

    def _sysex(self, command, address, data):
        body = list(address) + list(data)
        return [0xF0, 0x41, self.dev, *MODEL, command, *body, checksum(body), 0xF7]

    def _flush(self):
        while self.inp.get_message():  # drop anything stale
            pass
        self._partial = []

    def _receive(self):
        """Next complete message, or None. Windows delivers SysEx in pieces of at most 1024 bytes: join them."""
        msg = self.inp.get_message()
        if not msg:
            return None
        m = msg[0]
        if getattr(self, '_partial', None) or (m and m[0] == 0xF0 and m[-1] != 0xF7):
            self._partial = (getattr(self, '_partial', None) or []) + m
            if self._partial[-1] != 0xF7:
                return None
            m, self._partial = self._partial, []
        return m

    def _ask(self, command, address, data):
        """Send one message and return the data of the DT1 reply for the same address."""
        self._flush()
        self.out.send_message(self._sysex(command, address, data))
        head = [0xF0, 0x41, self.dev, *MODEL, DT1, *address]
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            m = self._receive()
            if not m:
                time.sleep(0.001)
                continue
            if m[:len(head)] == head and m[-1] == 0xF7:
                if checksum(m[8:-2]) != m[-2]:  # address + data, after F0 41 dev model(4) command
                    raise TR8SError('Corrupt reply from the TR-8S (checksum)')
                return m[len(head):-2]
        raise TR8SError(f'Timeout waiting for the TR-8S (address {bytes(address).hex()})')

    def _command(self, name, data=(0,), timeout=None):
        if timeout:
            old, self.timeout = self.timeout, timeout
            try:
                return self._ask(DT1, offset(UTILITY, [COMMAND[name]]), data)
            finally:
                self.timeout = old
        return self._ask(DT1, offset(UTILITY, [COMMAND[name]]), data)

    def _send(self, address, data):
        self.out.send_message(self._sysex(DT1, address, data))
        time.sleep(SEND_GAP)

    def _param(self, spec, index, step=0):
        base, size = spec
        return self._ask(RQ1, offset(offset(base, BLOCK, index), [0x01, 0x00], step), enc7(size, 4))

    # ---- reads ----

    def read_version(self):
        d = self._command('version')
        return bytes(d[:4]).decode('ascii', 'replace'), bytes(d[4:8]).decode('ascii', 'replace')

    def is_playing(self):
        return self._command('playing')[0] != 0

    def read_storage(self):
        d = self._command('freeArea')
        total, longest, top = dec7(d[0:4]), dec7(d[4:8]), dec7(d[8:12])
        return dict(free_bytes=total, longest_free=longest, top_free_address=top,
                    free_tones=dec7(self._command('freeToneCount')[:2]))

    def read_kit(self, k):
        """Kit slot k (0-127): name and the 11 pad tone ids (624+ = user sample US 1+)."""
        name = bytes(self._param(KIT_NAME, k)).split(b'\0')[0].decode('ascii', 'replace').strip()
        return dict(id=k, name=name, tones=[nibbles(self._param(KIT_TONE, k, i)[:4]) for i in range(11)])

    def read_tone(self, t):
        """User sample slot t (624-1023); 'loaded' False for an empty slot."""
        loaded = self._param(TONE_TYPE, t)[0] != 0
        out = dict(id=t, us=t - MIN_TONE + 1, loaded=loaded)
        if loaded:
            out.update(name=bytes(self._param(TONE_NAME, t)).split(b'\0')[0].decode('ascii', 'replace').strip(),
                       category=self._param(TONE_CATEGORY, t)[0],
                       address=nibbles(self._param(TONE_ADDRESS, t)), size=nibbles(self._param(TONE_SIZE, t)),
                       channels=self._param(TONE_CHANNEL, t)[0])
        return out

    # ---- changes (always inside `with dev.locked():`) ----

    @contextlib.contextmanager
    def locked(self, message='TR8orS-Kit-Loader'):
        """Panel locked (and a message on the TR-8S screen) while changes run; always unlocked afterwards."""
        if self.is_playing():
            raise TR8SError('Stop the TR-8S first.')
        if self._command('lock', [1])[0] != 0:
            raise TR8SError('The TR-8S refused to lock its panel.')
        try:
            time.sleep(0.5)
            self._command('display', list(message.encode('ascii', 'replace')[:32].ljust(32)))
            yield
        finally:
            self._command('lock', [0])

    def delete_tone(self, t, kits):
        """Delete user sample slot t (624-1023). Refuses samples any kit still uses (kits = read_kit results)."""
        if not MIN_TONE <= t <= MAX_TONE:
            raise TR8SError(f'{t} is not a user sample slot')
        users = [k['name'] for k in kits if t in k['tones']]
        if users:
            raise TR8SError(f'US {t - MIN_TONE + 1} is used by: {", ".join(users)}')
        self._send(offset(offset(TONE_CATEGORY[0], BLOCK, t), [0]), [1])  # Roland sets the category first
        if self._command('deleteTone', enc7(t, 2))[0] != 0:
            raise TR8SError(f'The TR-8S could not delete US {t - MIN_TONE + 1}')
        self._write('tone', t)
        if self.read_tone(t)['loaded']:
            raise TR8SError(f'US {t - MIN_TONE + 1} is still there after deleting')

    def delete_samples(self, ids, status=None):
        """Delete these user samples, also ones kits use: those pads first switch to the factory 909 sound for that
        pad, so no kit points at a deleted sample. Run inside locked(). Returns {kit slot: [pad index, ...]}."""
        ids, changed = set(ids), {}
        for k in range(128):
            pads = [i for i, t in enumerate(self.read_kit(k)['tones']) if t in ids]
            if pads:
                status and status(f'Kit slot {k + 1}: switching {len(pads)} pad(s) to built-in sounds…')
                body = bytearray(self.get_data('kit', k))
                for i in pads:
                    struct.pack_into('<H', body, t8k.INST_OFS + i * t8k.INST_STRIDE, t8k.DEFAULT_ROM[i][0])
                self.write_kit(k, bytes(body))
                changed[k] = pads
        kits = [self.read_kit(k) for k in range(128)]
        for n, t in enumerate(sorted(ids), 1):
            status and status(f'Deleting {n} / {len(ids)}: US {t - MIN_TONE + 1}…')
            self.delete_tone(t, kits)  # still refuses anything a kit uses
        return changed

    def optimize(self, progress=None):
        """Defragment the sample memory (can take minutes); progress(percent). Saves all tone slots afterwards."""
        address = offset(UTILITY, [COMMAND['optimize']])
        head = [0xF0, 0x41, self.dev, *MODEL, DT1, *address]
        self._flush()
        self.out.send_message(self._sysex(DT1, address, [0]))
        try:
            end, pct = time.monotonic() + 30, -1
            while pct < 100:
                if time.monotonic() > end:
                    raise TR8SError('The TR-8S stopped reporting Optimize progress')
                m = self._receive()
                if not m:
                    time.sleep(0.01)
                    continue
                if m[:len(head)] == head:
                    pct = m[len(head)]
                    if pct > 100:
                        raise TR8SError('The TR-8S reported an Optimize error')
                    progress and progress(pct)
                    end = time.monotonic() + 30  # Roland allows 30 s between progress messages
        finally:
            self._write('tone', 16383)  # all

    # ---- data transfers (Roland's chunked handshake) ----

    def _replies(self, timeout):
        """Yield (address, data) of DT1 messages from the TR-8S until `timeout` s pass without one."""
        head = [0xF0, 0x41, self.dev, *MODEL, DT1]
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            m = self._receive()
            if not m:
                time.sleep(0.001)
                continue
            if m[:len(head)] == head and m[-1] == 0xF7 and len(m) >= len(head) + 6:
                if checksum(m[8:-2]) != m[-2]:
                    raise TR8SError('Corrupt reply from the TR-8S (checksum)')
                yield m[8:12], m[12:-2]
                end = time.monotonic() + timeout
        raise TR8SError('The TR-8S stopped answering during a transfer')

    def send_data(self, kind, params, data, progress=None):
        """Transfer `data` to the TR-8S: kind in SEND, params like [slot, 1] or [address, length]."""
        start = offset(UTILITY, [SEND[kind]])
        prog = offset(UTILITY, PROGRESS)
        self._flush()
        self.out.send_message(self._sysex(DT1, start, [b for p in params for b in enc7(p, 4)]))
        pos = 0
        for address, reply in self._replies(10):
            if address == prog:
                if reply[0] > 100:
                    raise TR8SError(f'The TR-8S rejected the {kind} transfer')
                progress and progress(reply[0])
                if reply[0] == 100:
                    return
            elif address == start and reply[0] != 0:
                raise TR8SError(f'The TR-8S refused the {kind} transfer')
            if pos < len(data):  # any answer means: send the next chunk
                size = 1024
                while pos + size > len(data):
                    size >>= 1
                time.sleep(CHUNK_GAP)
                self.out.send_message(self._sysex(DT1, offset(UTILITY, [0x01, size.bit_length() - 1]),
                                                  pack7(data[pos:pos + size])))
                pos += size

    def get_data(self, kind, index, size=None):
        """Read raw bytes from the TR-8S: a kit (0-127) or tone / pcmTone (624-1023); for kind 'sample', index is
        a memory address and size the number of bytes."""
        size = size or SIZE[kind]
        start = offset(UTILITY, [GET[kind]])
        chunks = {dec7(offset(UTILITY, [0x01, k])) for k in range(11)}
        self._flush()
        self.out.send_message(self._sysex(DT1, start, enc7(index, 4) + enc7(size if kind == 'sample' else 1, 4)))
        out = bytearray()
        for address, reply in self._replies(10):
            if address == start:
                if reply[0] != 0:
                    raise TR8SError(f'The TR-8S refused to send {kind} {index}')
            elif dec7(address) in chunks:
                out += unpack7(reply)
                if len(out) >= size:
                    return bytes(out[:size])

    def _write(self, kind, index):
        self._send(offset(UTILITY, [WRITE[kind]]), enc7(index, 2))

    def write_kit(self, slot, body, progress=None):
        """Store a 1312-byte kit (t8k.kit_body) in kit slot 0-127 and save it; verified by reading it back."""
        if not 0 <= slot <= 127 or len(body) != SIZE['kit']:
            raise TR8SError('Bad kit slot or kit data')
        self.send_data('kit', [slot, 1], body, progress)
        self._write('kit', slot)
        got = self.get_data('kit', slot)
        # byte 4 of each pad is derived by the TR-8S from the pad's sound (hardware test), so it is not compared
        derived = {0x184 + i * 0x34 + 4 for i in range(11)}
        if any(g != b for k, (g, b) in enumerate(zip(got, body)) if k not in derived):
            raise TR8SError(f'Kit slot {slot + 1} does not match what was sent')

    def upload_sample(self, tone, pcmt, data, progress=None):
        """Put one sample (t8k.sample_record) into free memory and a free sample slot; returns its tone id.
        Roland's order: audio to the first free address, ask for a free slot, then its TONE and PCMT entries."""
        st = self.read_storage()
        addr = st['top_free_address']
        if st['free_tones'] < 1 or len(data) > st['longest_free'] or addr + len(data) - 1 > MAX_ADDRESS:
            raise TR8SError('Not enough free sample memory or slots on the TR-8S (Optimize may help)')
        self.send_data('sample', [addr, len(data)], data, progress)
        t = dec7(self._command('freeTone')[:2])
        if not MIN_TONE <= t <= MAX_TONE:
            raise TR8SError('The TR-8S has no free sample slot')
        self.send_data('tone', [t, 1], tone)
        right = addr + int.from_bytes(pcmt[8:12], 'little') if pcmt[28] > 1 else addr
        pcmt = addr.to_bytes(4, 'little') + right.to_bytes(4, 'little') + pcmt[8:]
        self.send_data('pcmTone', [t, 1], pcmt)
        self._write('tone', t)
        if self.get_data('tone', t) != bytes(tone) or self.get_data('pcmTone', t)[:8] != pcmt[:8]:
            raise TR8SError(f'US {t - MIN_TONE + 1} does not match what was sent')
        return t

    def find_sample(self, x, rate, tones):
        """Tone id of a sample already on the TR-8S that is identical to int16 array x (n, ch) at `rate`, or None.
        Only candidates with the same length and channels are compared, by reading their audio back."""
        n, ch = x.shape
        want = x.T.astype('<i2').tobytes()
        for t in tones:
            if t['size'] == n * 2 and t['channels'] == ch \
                    and int.from_bytes(self.get_data('pcmTone', t['id'])[24:28], 'little') == rate \
                    and self.get_data('sample', t['address'], len(want)) == want:
                return t['id']
        return None

    def read_all(self, progress=None):
        """Whole machine: version, free memory, 128 kits, every loaded user sample."""
        kits, tones = [], []
        for k in range(128):
            kits.append(self.read_kit(k))
            progress and progress((k + 1) / 528)
        for t in range(MIN_TONE, MAX_TONE + 1):
            x = self.read_tone(t)
            if x['loaded']:
                tones.append(x)
            progress and progress((129 + t - MIN_TONE) / 528)
        return dict(version=self.version, storage=self.read_storage(), kits=kits, tones=tones)
