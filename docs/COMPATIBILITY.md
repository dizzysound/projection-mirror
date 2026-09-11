# Compatibility

## What this project is actually verified against

**Two Panasonic PT-F300U projectors.** That is the entire verified surface. Everything else on
this page is *inference from Panasonic's own compatibility list*, not a claim that this code works.

Our units report `PanasonicF300NT` in their `602` discovery reply, which places them in the
**PT-FW300NT / PT-F300NT** group of Panasonic's list. The trailing destination letter (`E`, `EA`,
`U`, or none) does not change the specification, so PT-F300U and PT-F300NT are the same device for
our purposes.

## Panasonic's official list

Source: *List of Compatible Device Models — Wireless Manager ME6.4 / Wireless Manager mobile
edition 6.4*, as of November 2018 (Panasonic doc `TQDJ19172-11`). Models are grouped, and **each
group has a different feature table**.

| Group | Models |
|---|---|
| A | PT-MZ770, PT-MZ670, PT-MZ570, PT-MW730, PT-MW630, PT-MW530 |
| B | PT-DZ570, PT-DW530, PT-DX500, PT-FW430, PT-FX400 |
| C | PT-VZ585N, PT-VW545N, PT-VX615N |
| D | PT-VZ575N, PT-VW535N, PT-VX605N, PT-VW355N, PT-VX425N |
| **E** | **PT-F300NT, PT-FW300NT**  ← this project's target |
| F | PT-VW435N, PT-VX505N |
| G | PT-VW345N, PT-VX415N |
| H | PT-VX400NT |
| I | PT-LB75NT, PT-LB80NT, PT-LB90NT, PT-LW80NT |

Panasonic states the software is **not** compatible with any projector model not on that list.

### Feature profile for our group (PT-F300NT / PT-FW300NT)

| Feature | Supported |
|---|---|
| `[S-MAP]`, `[USER1]`–`[USER2]`, `[1-4]`, `[IP designation]` connection | yes |
| `[SIMPLE]`, `[S-DIRECT]` / `[M-DIRECT]`, `[USB display]` connection | no |
| Live mode | yes |
| Multi-Live mode | yes |
| **Multiple unit live mode** | **yes** |
| WEB control | yes |
| Moderator mode, Browser remote control, Content Manager | no |

Two things worth drawing out:

- **"Multiple unit live mode: yes"** is exactly what this project does — one Mac driving both
  projectors at once. Our dual-sender design matches a mode the hardware officially supports.
- **"Browser remote control: no"** on this family. The `remote_url()` helper added in v1.5 points
  at `/cgi-bin/remocon.cgi`; Panasonic lists that feature as unsupported here, so treat it as
  unverified on F300NT hardware even though *WEB control* is supported.

## Flat-panel displays

These models were supplied separately and do **not** appear in the ME6.4 projector list above:

```
TH-80BF1    TH-65BF1    TH-50BF1
TH-80LFB70  TH-65LFB70  TH-50LFB70
TH-80LFC70  TH-65LFC70  TH-50LFC70
```

Panasonic's professional displays of that era took the same Wireless Manager transport, so the
protocol in [PROTOCOL.md](PROTOCOL.md) is *plausibly* applicable. It is untested here, and their
compatibility is documented in a separate Panasonic list, not the one cited above.

## What would actually be needed to support another model

**Geometry is no longer hardcoded.** The client reads the projector's real resolution from the
handshake and encodes for it:

```
-> STAT RES
<- 112 1024 768
```

`set_panel()` / `adopt_panel()` take that reply and drive the scale target, the strip layout, and
the panel size written into every strip header. A panel height that is not a multiple of the
96-pixel strip height simply gets a shorter final strip, since each strip carries its own height
and offset. The 1024×768 path is unchanged — verified byte-identical on the wire against the
pre-change encoder.

Two caveats remain, and both need hardware to settle:

- **Codec 3 on other panels.** Quality is pinned at 50 because the F300 decodes with its own
  built-in quantization tables (see [PROTOCOL.md](PROTOCOL.md)). Whether another model's built-in
  tables imply the same quality is unverified.
- **Mixed resolutions in one session.** A frame is encoded once and shared by every projector, so
  projectors of differing resolutions cannot be served from one stream. `adopt_panel()` warns and
  encodes for the first. Supporting a mix would need per-resolution encoding.

The association key derivation (see [CRYPTO.md](CRYPTO.md)) is model-independent: it depends only
on the device's location string, so a display or another projector would derive its key the same
way.
