# Support HCI_LE_Create_BIG and HCI_LE_Terminate_BIG (LE Audio Broadcaster)

## Summary

This change adds end-to-end support for the **Isochronous Broadcaster** role in
RootCanal, so that a host stack (e.g. Google Bumble) can set up an **LE Audio
Broadcast Transmitter**. Two HCI commands that were previously not wired up are
now supported and dispatched to the link layer:

- `HCI_LE_Create_BIG` (OCF `0x0068`, opcode `0x2068`)
- `HCI_LE_Terminate_BIG` (OCF `0x0069`, opcode `0x2069`)

When a BIG is created on a periodic advertising train, the controller now also
announces the **BIGInfo** on that train, which is required for a real
Auracast/LE Audio broadcaster. Creating the BIG does not change the periodic
advertising schedule; instead the periodic advertising PDU (AUX_SYNC_IND)
continues and carries the BIGInfo in its **ACAD** field (and the advertising
data can carry the BASE). Receivers use that BIGInfo to find and synchronize to
the BIG.

Only the **broadcaster** side is implemented in this diff. The complementary
*Synchronized Receiver* role (`HCI_LE_BIG_Create_Sync` /
`HCI_LE_BIG_Terminate_Sync` and consuming the BIGInfo to establish a BIG sync)
is intentionally left out of scope and will be done later.

## Background

The Rust link layer already contained `hci_le_create_big` / `hci_le_terminate_big`
handlers and the HCI command/event packet definitions, but they were **not
reachable** end-to-end:

- The C++ controller did not advertise `LE_CREATE_BIG` / `LE_TERMINATE_BIG` as
  supported commands, and did not set the `Isochronous Broadcaster` LE feature
  bit, so `DualModeController` rejected both commands with
  `UNKNOWN_HCI_COMMAND` (`0x01`).
- The BIGInfo was never announced on the periodic advertising train.

## What changed

### Controller properties / HCI command support

- `model/controller/controller_properties.cc`
  - Added `LLFeaturesBits::ISOCHRONOUS_BROADCASTER` to the default `LlFeatures()`.
  - Enabled `OpCodeIndex::LE_CREATE_BIG` and `OpCodeIndex::LE_TERMINATE_BIG` in
    the default `SupportedCommands()` mask.
  - Added an `le_isochronous_broadcast_commands_` list and applied it from the
    new `features.le_isochronous_broadcast` configuration toggle.
- `proto/rootcanal/configuration.proto`
  - Added `le_isochronous_broadcast` to `ControllerFeatures` for explicit enable /
    disable (defaults to enabled).

### HCI event layout fix for Bumble interoperability

- `packets/hci_packets.pdl`
  - Changed `HCI_LE_Create_BIG_Complete`'s connection-handle prefix from
    `_size_(connection_handle)` (byte length, i.e. `num_bis * 2`) to
    `_count_(connection_handle)` (item count, i.e. `num_bis`). The Core spec only
    defines `Num_BIS` (a count), and Bumble expects that count, so this makes the
    `LE Create BIG Complete` event (and hence BIS handles) parse correctly in
    Google Bumble.

### BIGInfo on the periodic advertising train (ACAD field)

- `packets/link_layer_packets.pdl`
  - Added an `acad` field to `LePeriodicAdvertisingPdu` so the periodic
    advertising PDU (AUX_SYNC_IND) carries an ACAD payload. When a BIG is
    associated with the train, the controller fills the ACAD with the BIGInfo
    AD structure; the advertising data (BASE) continues to be carried as before.
- `rust/src/llcp/iso.rs`
  - Added a `BigInfo` type and `impl BigConfig::big_info()`, deriving the BIGInfo
    fields (NSE, BN, PTO, IRC, Max_PDU, ISO_interval, …) with the same parameter
    derivation used by `HCI_LE_Create_BIG`.
  - Added `IsoManager::get_big_info(advertising_handle)` to look up the BIGInfo
    to announce.
- `rust/src/llcp/manager.rs`
  - Exposed `LinkLayer::get_big_info` to the FFI.
- `rust/src/ffi.rs` and `rust/include/rootcanal_rs.h`
  - Added a `BigInfoFfi` struct and the `link_layer_get_big_info` C ABI used by
    the controller to query the link layer.
- `model/controller/le_controller.cc` / `le_controller.h`
  - Added `LeController::BuildLeBigInfoAcad`, which queries the link layer for
    the advertising handle and encodes the BIGInfo as an ACAD AD structure
    (AD Type `0x2C`, cf Vol 6, Part B § 1.3.1).
- `model/controller/le_advertiser.cc` / `le_advertiser.h`
  - Wired a `periodic_acad_builder` callback on each extended advertiser,
    invoked while building each periodic advertising PDU, so that the ACAD field
    carries the BIGInfo of any BIG associated with the train.

## Testing

A Google Bumble integration test was added and validated end-to-end against a
running RootCanal instance over a TCP HCI transport:

- `examples/python/broadcast_transmitter.py` (with `examples/python/pyproject.toml`)
  - Connects to RootCanal via `tcp-client:127.0.0.1:6402`.
  - Verifies the `Isochronous Broadcaster` feature and both commands are
    advertised.
  - Sets up a periodic advertising train, creates a BIG with 2 BIS, configures the
    ISO data path on one BIS, and terminates the BIG.

Run it with:

```sh
bazel run //:rootcanal   # in one terminal (HCI on 6402)

cd examples/python && uv run --with bumble python broadcast_transmitter.py
```

Observed output:

```
Isochronous Broadcaster LE feature: True
HCI_LE_Create_BIG supported:       True
HCI_LE_Terminate_BIG supported:    True
...
Creating BIG...
BIG #0 established:
  BIS connection handles : [3328, 3329]
  ISO_Interval           : 10.0 ms
ISO data path configured on BIS 3328 (host -> controller)
BIG terminated.
```

During testing the RootCanal controller was observed embedding the BIGInfo
AD structure into the periodic advertising PDU's ACAD field on every periodic
advertising event once the BIG existed (confirmed via a debug trace added
temporarily while developing).

## Notes / future work

- The *Synchronized Receiver* role is not implemented here and will be done
  later: `HCI_LE_BIG_Create_Sync` / `HCI_LE_BIG_Terminate_Sync`, parsing the
  BIGInfo carried in the received periodic advertising ACAD, and generating the
  corresponding HCI LE BIGInfo Advertising Report event.
- Broadcasting BIS SDUs out on the air interface is not part of this change;
  the host can already configure the ISO data path for a BIS, which is the
  prerequisite for a functional transmitter.