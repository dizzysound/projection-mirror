# The PT-F300U network display protocol

Everything here was confirmed against live hardware and packet captures, unless marked otherwise.

## Ports

| Port | Transport | Purpose |
|---|---|---|
| 4352 | TCP | PJLink — power, input, AV-mute. Independent of everything below. |
| 10000 | UDP | Discovery (`600`/`602`) and association (`500`/`501`, `700`) |
| 12000 | TCP | Control channel — session setup, `NOOP` keepalive, `QUIT` |
| dynamic | TCP | Video channel. The port is assigned by the control channel (`120 OK <port>`). |
| 12004 | UDP | NOT USED. WM binds a UDP socket here but never sends to the projector; a per-frame datagram here wedges the module (see FINDINGS) |

A projector serves **one session slot**. A second client, or a stale half-open session, is refused.

## 1. Discovery — UDP 10000

Client broadcasts (or unicasts) a 23-byte probe:

```
"600TRANSPIC0430" + <8-char token>
```

Each projector replies with a 106-byte `602` record. Field offsets, verified against capture:

| Offset | Length | Field | Example |
|---|---|---|---|
| 0 | 3 | Literal `602` | `602` |
| 3 | 19 | Model, space-padded | `PanasonicF300NT    ` |
| 38 | 16 | **Location, space-padded** | `RIGHT           ` |
| 54 | 8 | Name | `Proj0001` |

The **location field at offset 38 is the input to key derivation** — not the `ProjNNNN` name.
See [CRYPTO.md](CRYPTO.md).

Discovery also wakes a dormant projector so it will answer association.

## 2. Association — UDP 10000

The client sends three packets repeatedly (~10 Hz) until the projector answers:

- `700…` — a fixed token packet
- `500` packet — `b"500043001" + AES_ECB_encrypt(pkey, bswap32(pkey))`
- `600TRANSPIC0430\x00` + 114 zero bytes

The projector replies `501`, carrying a 16-byte blob at offset 9. From it:

```
m_CommKey = bswap32( AES_ECB_decrypt(pkey, blob) )
```

`pkey` is constant per projector; `m_CommKey` is fresh per session. `bswap32` reverses each
4-byte word.

## 3. Control channel — TCP 12000

Plain text. The real Wireless Manager sends each command and **waits for its reply** before the
next; pipelining all three at once also works.

```
-> INIT 07 RT 1024 768 <token>
<- 104 OK 0 TCP
-> STAT RES
<- 112 1024 768
-> TYPE TCP none none
<- 120 OK 1029                 <- the video port
```

The `112` reply is the projector's **real panel resolution**, and the client adopts it: strip
geometry, the scale target, and the panel size in every strip header all follow from it. The
`INIT` line still requests 1024×768, because the resolution is not known until the projector
answers.

Then a `NOOP` **every ~4 seconds for the life of the session**, and `QUIT` at the end
(answered `182 OK`).

> **The `NOOP` keepalive is mandatory.** With send-on-change video, a static screen transmits no
> frames. Without an independent keepalive the control channel goes idle, the projector times the
> session out, and the network module wedges. This was the single most damaging bug in development.

`QUIT -> 182 OK` means a clean teardown. `QUIT -> (no reply)` means the module has already hung.

## 4. Video channel — dynamic TCP port

A frame is a column of horizontal strips sent top to bottom, sized from the negotiated
resolution. On a 1024×768 panel that is exactly 8 strips of 1024×96. A height that is not a
multiple of the strip height gets a shorter final strip — every strip carries its own height and
Y offset in the header, so the projector does not require uniform strips. Each strip is a 24-byte header
followed by an encrypted payload.

### Strip header (24 bytes, big-endian)

| Bytes | Value | Meaning |
|---|---|---|
| 0–1 | `07 01` | Magic |
| 2 | `00` / `80` | `0x80` = **lastsend**: triggers the projector to render |
| 4–5 | `03 10` | Codec 3 (JPEG). Byte 5 must be `0x10`. |
| 6–7 | `00 14` | Constant |
| 8–11 | u32 | Payload length |
| 12–13 | u16 | Strip width — the panel width |
| 14–15 | u16 | Strip height — `96`, or less for a final remainder strip |
| 18–19 | u16 | Y offset of this strip |
| 20–21 | u16 | Panel width, from the `112` reply |
| 22–23 | u16 | Panel height, from the `112` reply |

Only the **last strip in a batch** carries `lastsend`. Send a subset of strips and the projector
composites them into its framebuffer, which is what makes delta encoding possible.

### Payload encryption

```
AES-128-ECB(m_CommKey) over the leading (len // 16) * 16 bytes,
then the remaining len % 16 bytes appended in PLAINTEXT.
```

### Payload: codec 3

A **standard, stuffed, baseline JPEG entropy-coded scan** — everything from after the SOS header
to the closing `FF D9`, with `FF 00` byte stuffing left intact.

The quantization, Huffman and frame headers are **stripped and never transmitted**. The projector
decodes using its own fixed built-in tables. Three consequences:

- **JPEG quality is locked at 50.** Encoding at any other quality misscales the coefficients
  against the projector's fixed tables and appears as a *brightness shift*, not a quality change.
- `optimize=False` is required, so libjpeg emits the standard Huffman tables.
- Chroma subsampling is fixed at 4:2:0.

After a batch: nothing. The control channel carries `NOOP` on a ~4 s timer only (WM: 4.0 s on Mac,
3 s on Android). **Never send a per-frame NOOP or a UDP datagram to 12004** - both were in
early versions of this project and they exhaust a ~3300-command budget in the projector's
network module, which then hangs until a physical power cycle. The 700 beacon is a burst after
association, then one 700 every ~0.5 s (a separate process holds this cadence so encode load cannot
starve it); it keeps the NETWORK input-guide OSD suppressed. The 500 is connect-time only - a
periodic post-association 500 re-selects the input and pops the OSD. The 600 discovery broadcast is
pre-association only.

## 5. Teardown

```
send a black frame  ->  shutdown() the video socket  ->  QUIT / 182 OK  ->  shutdown() control
```

The black frame stops the projector holding the last mirrored image. Abandoning a send mid-frame
instead of closing cleanly is what leaves the module hung.
