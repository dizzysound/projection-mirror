# Key derivation

Two keys matter.

| Key | Scope | How it is obtained |
|---|---|---|
| `pkey` | **Constant per projector**, survives power cycles | Derived from the projector's location name |
| `m_CommKey` | Fresh per session | `bswap32(AES_ECB_decrypt(pkey, blob_from_501))` |

`pkey` authenticates association; `m_CommKey` encrypts every video strip.

## The per-projector key, reimplemented

`src/pm_pkey.py` is a pure-Python, independent reimplementation of the key derivation — no
native library, no `ctypes`, no capture step. It reproduces, from protocol analysis, the
deterministic function that turns a projector's location string into its `pkey`. No vendor code is
used or included.

### The input is the LOCATION field

```
create_temp_key("RIGHT") -> 45dd79332116f723c156db7c9ef076d7
create_temp_key("LEFT")  -> c41eebe7bac4340ad510533592347e64
```

The input is the **16-character space-padded location field at offset 38 of the `602` discovery
reply** — for example `RIGHT` / `LEFT` — *not* the `ProjNNNN` name at offset 54.

This one assumption cost the most time in the whole project. An earlier attempt used the
`ProjNNNN` device name as the input, could never make it fit, and concluded the derivation was too
complex to finish. The math transcribed straightforwardly; the assumed input was simply wrong.

The tell is that the derivation **converts trailing spaces to `*`**, which only makes sense for a
fixed-width space-padded field. Padding is therefore irrelevant — `"LEFT"`, `"LEFT "` and
`"LEFT            "` all produce the same key.

### Algorithm

```
host20 = name truncated to 16 bytes, padded to 20 with '*', trailing spaces -> '*'
temp   = SALT xor host20                       SALT = "12345678901234567890"
seed   = byteswap each 4-byte word of temp     (5 words)

seed_prng(state, seed, 5):
    key[16] = seed words, remaining words filled with 0x9cd0d89d
    state   = A[17 words] + B[1024 bytes], both zeroed
    2 priming rounds over the two 32-byte halves of key
    then 32 rounds, each fed from B[0x80:0xa0]:
        mix1(B, block)
        mix2(A, block, aux = B[0x200:0x220])

pkey = take(state, 4) = A[9:13] as little-endian uint32
```

**`mix1`** — a 1024-byte shift register. Shifts the whole buffer up by `0x20`, XORs the evicted
32-byte tail (rotated left 8 bytes) into `B[0x320]`, and writes `evicted ^ block` at `B[0x00]`.

**`mix2`** — a nonlinear step over 17 words, in three passes plus a mix-in:

```
A[i] = ((~T[(i+2) % 17]) | T[(i+1) % 17]) ^ T[i]
A[k] = rotl32(T[(7k) % 17], ((k*(k+1)) & 0xFF) >> 1)      # stride-7 permute, data-dependent rotate
A[k] ^= T[(4+k) % 17] ^ T[(1+k) % 17]
A[0] ^= 1;  A[1..8] ^= block;  A[9..16] ^= aux
```

`take` emits `A[9..16]` per 8-word block **before** mixing; a 4-word request returns `A[9..12]`.

## Verification

The derivation reproduces exactly for every projector tested, and the full chain was validated
offline: `602` replies parsed, location extracted, key derived, each matching the value the
projector accepts at association.

`tests/test_offline.py` re-checks the vectors, the `host20` edge cases, and the `602` offsets.

## Why this matters operationally

Adding or replacing a projector needs **no capture and no code change**. `pkey_for(ip)` returns a
baked constant when one is known and otherwise runs discovery and derives the key. The baked
`PKEYS` table is only a cache — it avoids a discovery round-trip and still works if discovery is
blocked.
