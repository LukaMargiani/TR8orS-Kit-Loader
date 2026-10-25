"""Roland TR-8S kit file (.t8k) reader/writer.

Layout (reverse-engineered from a device export, all little-endian):
  header 0x40: 'T8K ', 0, chunk count, 0, 16 spaces, 12-byte device id, 12 zero bytes,
               crc32(all chunk headers), crc32(header[:0x3c])
  chunk:       tag, 0, body size, 0, item count, item size, crc32(body) (0 for WAVE), crc32(chunk header[:28])
  chunks:      NAME (16 chars), KIT (1312 bytes of kit params), TONE (36 bytes/sample),
               PCMT (64 bytes/sample), WAVE (one SMPL record per sample)
  SMPL:        'SMPL', block size, crc32(data), crc32(record header[:12]), 16-bit PCM planar (L then R),
               zero-padded to whole 128 KB blocks
"""
import os
import re
import struct
import zlib

import numpy as np

SLOTS = ['BD', 'SD', 'LT', 'MT', 'HT', 'RS', 'HC', 'CH', 'OH', 'CC', 'RC']
RATE = 44100
USER_TONE = 0x270                      # kit tone id of the file's first user sample ("US 1")
INST_OFS, INST_STRIDE = 0x184, 0x34    # u16 tone id of each slot inside the KIT body
COLOR_OFS = 0x2A                       # 11 bytes, one per slot: index into COLORS (verified on all 128 factory kits)
COLORS = ['Red', 'Orange', 'Yellow', 'Lime', 'Green', 'SkyBlue', 'LightBlue', 'Blue', 'Purple', 'Magenta', 'Pink', 'White']
DEFAULT_COLORS = [0, 1, 3, 3, 3, 1, 1, 2, 2, 2, 2]
# user sample categories (TONE entry byte 16), in the TR-8S's own order
CATEGORIES = ['IMPORT', 'BD', 'SD', 'TOM', 'RS', 'HC', 'CH/OH', 'CC/RC', 'PERC1', 'PERC2', 'PERC3', 'PERC4', 'PERC5',
              'FX/HIT', 'VOICE', 'SYNTH1', 'SYNTH2', 'BASS', 'SCALED', 'CHORD', 'OTHERS']
# built-in (ROM) tones of the factory TR-909 kit, per slot: what an empty pad plays (no user memory used)
DEFAULT_ROM = [(27, '909 Bass2'), (31, '909 Snare2'), (33, '909 Low Tom'), (34, '909 Mid Tom'), (35, '909 High Tom'),
               (36, '909 Rim Shot'), (37, '909 Hand Clap'), (38, '909 Closed HH'), (39, '909 Open HH'),
               (40, '909 Crash Cymbal'), (41, '909 Ride Cymbal')]
BLOCK = 0x20000
BASE = 0x00D00000   # first user sample address in TR-8S memory; the TR-8S places imported samples itself
DEVID = bytes(12)   # 12-byte source id in kit files; neutral (the TR-8S re-assigns everything on import)


def _name(s):
    return s.upper().encode('ascii', 'replace')[:16].ljust(16)


def _chunk(tag, count, item, body, body_crc=True):
    h = tag + struct.pack('<6I', 0, len(body), 0, count, item, zlib.crc32(body) if body_crc else 0)
    return h + struct.pack('<I', zlib.crc32(h)), body


def read(path):
    """Returns (kit name, slots, meta). A slot is (name, int16 array (n, ch), rate) or (name, rom tone id, None)."""
    d = open(path, 'rb').read()
    if d[:4] != b'T8K ' or zlib.crc32(d[:0x3c]) != struct.unpack_from('<I', d, 0x3c)[0]:
        raise ValueError('Not a valid TR-8S kit file (.t8k)')
    chunks, p = {}, 0x40
    for _ in range(struct.unpack_from('<I', d, 8)[0]):
        size, _, count, _, crc, hcrc = struct.unpack_from('<6I', d, p + 8)
        body = d[p + 32:p + 32 + size]
        if hcrc != zlib.crc32(d[p:p + 28]) or len(body) != size or (crc and crc != zlib.crc32(body)):
            raise ValueError(f'Corrupt chunk {d[p:p + 4]!r} in kit file')
        chunks[d[p:p + 4]] = (count, body)
        p += 32 + size

    kit = chunks[b'KIT '][1]
    ntone, tone = chunks[b'TONE']
    pcmt, wave = chunks[b'PCMT'][1], chunks[b'WAVE'][1]
    samples, q = [], 0
    for i in range(ntone):
        name = tone[i * 36:i * 36 + 16].decode('ascii', 'replace').rstrip()
        end, _, rate, ch = struct.unpack_from('<3IB', pcmt, i * 64 + 16)
        n = end + 1
        a = np.frombuffer(wave, '<i2', ch * n, q + 16).reshape(ch, n).T.copy()
        samples.append((name, a, rate))
        q += 16 + struct.unpack_from('<I', wave, q + 4)[0]

    rom = {}  # ROM slot names only live in the companion .txt export
    txt = os.path.splitext(path)[0] + '.txt'
    if os.path.exists(txt):
        rom = dict(re.findall(r'^; (\w\w) "(.*?)"', open(txt, encoding='ascii', errors='replace').read(), re.M))
    slots = []
    for i, s in enumerate(SLOTS):
        t = struct.unpack_from('<H', kit, INST_OFS + i * INST_STRIDE)[0]
        k = t - USER_TONE
        slots.append(samples[k] if 0 <= k < ntone else (rom.get(s, f'ROM TONE {t}').rstrip(), t, None))
    meta = dict(kit=kit, devid=d[0x20:0x2c], base=struct.unpack_from('<I', pcmt, 0)[0] if ntone else BASE,
                colors=[min(c, 11) for c in kit[COLOR_OFS:COLOR_OFS + 11]])
    return chunks[b'NAME'][1].decode('ascii', 'replace').rstrip(), slots, meta


def kit_body(name, tones, colors=DEFAULT_COLORS, kit=None):
    """The 1312-byte KIT data (same in .t8k files and over USB): `kit` holds the kit's parameters (from an opened
    kit, or read from the user's own TR-8S; none ship with the app), with name, pad colors and pad tone ids set."""
    if not kit or len(kit) != 1312:
        raise ValueError('Kit parameters needed: open a .t8k, or read the TR-8S over USB once')
    kit = bytearray(kit)
    kit[:16] = _name(name)
    kit[COLOR_OFS:COLOR_OFS + 11] = bytes(colors)
    for i, t in enumerate(tones):
        struct.pack_into('<H', kit, INST_OFS + i * INST_STRIDE, t)
    return bytes(kit)


def sample_record(name, x, rate, addr, devid=DEVID, category=0):
    """One user sample as the TR-8S stores it: (TONE entry, PCMT entry, audio). x is int16 (n, ch); the audio is
    planar (L then R) and zero-padded to whole 128 KB blocks; addr is where it sits in sample memory."""
    n, ch = x.shape
    data = x.T.astype('<i2').tobytes()
    data = data.ljust(-(-len(data) // BLOCK) * BLOCK, b'\0')
    tone = _name(name) + bytes([category, 2]) + bytes(18)
    pcmt = struct.pack('<7I2B2x', addr, addr + (n * 2 if ch == 2 else 0), n * 2, 0, n - 1, n - 1, rate, ch, 0x12)
    return tone, pcmt + devid + bytes(20), data


def write(path, name, slots, kit=None, devid=DEVID, base=BASE, colors=DEFAULT_COLORS):
    """slots: 11 x (name, int16 array (n, ch), sample rate) or (name, rom tone id, None).
    colors: 11 indices into COLORS. Also writes the .txt."""
    tone, pcmt, wave, addr, users, ids = [], [], [], base, [], []
    for i, (sname, x, rate) in enumerate(slots):
        if isinstance(x, int):
            ids.append(x)
            continue
        t, p, data = sample_record(sname, x, rate, addr, devid)
        tone.append(t)
        pcmt.append(p)
        h = b'SMPL' + struct.pack('<II', len(data), zlib.crc32(data))
        wave += [h, struct.pack('<I', zlib.crc32(h)), data]
        ids.append(USER_TONE + len(users))
        users.append(i)
        addr += len(data)
    kit = kit_body(name, ids, colors, kit)

    c = len(users)
    if not c:  # hardware test: the TR-8S fails to read a kit without samples and then cannot write the card
        raise ValueError('A kit needs at least one sample; the TR-8S cannot read kits without audio')
    parts = [_chunk(b'NAME', 1, 16, _name(name)), _chunk(b'KIT ', 1, len(kit), kit),
             _chunk(b'TONE', c, 36, b''.join(tone)), _chunk(b'PCMT', c, 64, b''.join(pcmt)),
             _chunk(b'WAVE', c, 0, b''.join(wave), body_crc=False)]
    head = b'T8K ' + struct.pack('<3I', 0, len(parts), 0) + b' ' * 16 + devid + bytes(12)
    head += struct.pack('<I', zlib.crc32(b''.join(h for h, _ in parts)))
    head += struct.pack('<I', zlib.crc32(head))
    with open(path, 'wb') as f:
        f.write(head)
        for h, b in parts:
            f.write(h + b)

    nm = lambda s: _name(s).decode()
    lines = ['; Kit List  Version 1.00', '', '[KIT]', f'1 = "{nm(name)}"']
    for i, (sname, x, _) in enumerate(slots):
        lines.append(f'; {SLOTS[i]} "{nm(sname)}"' + (f'\t(US {users.index(i) + 1})' if i in users else ''))
    lines += [';', '', '[USER SAMPLE TONE]'] + [f'{k + 1} = "{nm(slots[i][0])}" ;\tIMPORT' for k, i in enumerate(users)]
    with open(os.path.splitext(path)[0] + '.txt', 'w', encoding='ascii', errors='replace', newline='\r\n') as f:
        f.write('\n'.join(lines) + '\n')
