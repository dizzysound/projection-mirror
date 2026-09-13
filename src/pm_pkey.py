#!/usr/bin/env python3
"""
pm_pkey.py - derive a PT-F300U's association key (pkey), pure-Python
from its name, with no native library.

The key is a deterministic function of the projector's 16-char space-padded LOCATION
field: a small seeded PRNG expands it into a key buffer. Independent reimplementation from
protocol analysis; no vendor code is used or included.

THE INPUT IS THE PROJECTOR'S LOCATION FIELD, NOT ITS "ProjNNNN" NAME.
An earlier attempt assumed the input was the "Proj0001" device name and could never make it fit; the
actual input is the 16-char space-padded location field from the UDP-10000 "602" discovery
reply - "RIGHT" / "LEFT" here. Trailing spaces are converted to '*', so the field's padding
does not matter: "LEFT", "LEFT " and "LEFT            " all give the same key.

Verified against both projectors:
  RIGHT -> 45dd79332116f723c156db7c9ef076d7
  LEFT  -> c41eebe7bac4340ad510533592347e64

Algorithm:
  host20 = name truncated to 16, padded to 20 with '*', trailing spaces -> '*'
  seed   = byteswap each 4-byte word of (SALT xor host20),  SALT = "12345678901234567890"
  expand the seed into the state; pkey = state.A[9:13] as little-endian uint32
"""
import struct

M32 = 0xffffffff
SALT = b"12345678901234567890"
FILL = 0x9cd0d89d          # pads the 16-word key buffer


def _rotl32(x, r):
    r &= 31
    return x if r == 0 else ((x << r) | (x >> (32 - r))) & M32


def _mix1(B, block):
    """1024-byte shift register: shifts up 0x20 and feeds back the evicted tail."""
    L = bytes(B[0x3e0:0x400])
    B[0x20:0x400] = B[0x00:0x3e0]
    F = L[8:32] + L[0:8]                       # evicted tail rotated left 8 bytes
    for i in range(0x20):
        B[0x320 + i] ^= F[i]
    for i in range(0x20):
        B[i] = L[i] ^ block[i]


def _mix2(A, blk, aux):
    """Nonlinear step over 17 words, then XOR in the block and aux."""
    T = A[:]
    for i in range(17):                        # ((~T[i+2]) | T[i+1]) ^ T[i]
        A[i] = (((~T[(i + 2) % 17]) & M32) | T[(i + 1) % 17]) ^ T[i]
    T = A[:]
    for k in range(17):                        # stride-7 permute + data-dependent rotate
        A[k] = _rotl32(T[(7 * k) % 17], ((k * (k + 1)) & 0xFF) >> 1)
    T = A[:]
    for k in range(17):
        A[k] ^= T[(4 + k) % 17] ^ T[(1 + k) % 17]
    A[0] ^= 1
    for j in range(8):
        A[1 + j] ^= blk[j]
        A[9 + j] ^= aux[j]


def _seed_state(seed_words, n):
    """Seed the state. Returns (A: 17 words, B: 1024 bytes)."""
    key = [0] * 16
    for i in range(min(n, 16)):
        key[i] = seed_words[i]
    for i in range(n, 16):
        key[i] = FILL
    A, B = [0] * 17, bytearray(0x400)
    kb = struct.pack("<16I", *key)
    for half in (0, 1):                        # two 32-byte halves of the key buffer
        aux = list(struct.unpack_from("<8I", B, 0x200))
        blk = kb[half * 0x20: half * 0x20 + 0x20]
        _mix1(B, blk)
        _mix2(A, list(struct.unpack("<8I", blk)), aux)
    for _ in range(32):                        # 32 rounds fed from B itself
        aux = list(struct.unpack_from("<8I", B, 0x200))
        blk = bytes(B[0x80:0xa0])
        _mix1(B, blk)
        _mix2(A, list(struct.unpack("<8I", blk)), aux)
    return A, B


def build_host20(name, declared_len=None):
    """The 20-byte key buffer: truncate/pad the location, with trailing-space handling."""
    nb = name.encode() if isinstance(name, str) else bytes(name)
    L = len(nb) if declared_len is None else declared_len
    buf = bytearray(21)
    if L != 0:
        i = 1
        while True:
            if i - 1 < len(nb):
                buf[i - 1] = nb[i - 1]
            if i >= L or i >= 16:              # copies at most 16 bytes
                break
            i += 1
    if L <= 19:
        for j in range(L, 20):
            buf[j] = 0x2a                      # '*'
    if 1 <= L <= 20 and buf[L - 1] == 0x20:    # trailing spaces -> '*'
        k = L - 1
        while k >= 0 and buf[k] == 0x20:
            buf[k] = 0x2a
            k -= 1
    return bytes(buf[:20])


def create_temp_key(name, declared_len=None):
    """pkey (16 bytes) for a projector whose location field is `name` (e.g. "RIGHT")."""
    h = build_host20(name, declared_len)
    temp = bytes(SALT[i] ^ h[i] for i in range(20))
    seed = b"".join(temp[i:i + 4][::-1] for i in range(0, 20, 4))   # bswap per word
    A, _ = _seed_state(list(struct.unpack("<5I", seed)), 5)
    return struct.pack("<4I", *A[9:13])


KNOWN_VECTORS = {"RIGHT": "45dd79332116f723c156db7c9ef076d7",
                 "LEFT":  "c41eebe7bac4340ad510533592347e64"}


def selftest():
    ok = True
    for nm, exp in KNOWN_VECTORS.items():
        got = create_temp_key(nm).hex()
        ok &= (got == exp)
        print("  %-6s -> %s  %s" % (nm, got, "ok" if got == exp else "FAIL exp=" + exp))
    return ok


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        for a in sys.argv[1:]:
            print("%s -> %s" % (a, create_temp_key(a).hex()))
    else:
        print("key-derivation self-test:")
        raise SystemExit(0 if selftest() else 1)
