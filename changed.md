# Support HCI_LE_Create_BIG / HCI_LE_Terminate_BIG and the Synchronized Receiver (LE Audio)

## Summary

This change adds end-to-end support for both **LE Audio** isochronous roles in
RootCanal, so that a host stack (e.g. Google Bumble) can set up an **LE Audio
Broadcast Transmitter** and a **Synchronized Receiver** that joins the broadcast.

**Broadcaster** commands previously wired up but not reachable are now supported:

- `HCI_LE_Create_BIG` (OCF `0x0068`, opcode `0x2068`)
- `HCI_LE_Terminate_BIG` (OCF `0x0069`, opcode `0x2069`)

**Synchronized Receiver** commands are newly implemented/removed from the disabled
list and routed to the link layer:

- `HCI_LE_Set_Periodic_Advertising_Receive_Enable` (OCF `0x2059`)
- `HCI_LE_BIG_Create_Sync` (OCF `0x206b`)
- `HCI_LE_BIG_Terminate_Sync` (OCF `0x206c`)

When a BIG is created on a periodic advertising train, the controller announces
the **BIGInfo** on that train: the periodic advertising PDU (AUX_SYNC_IND)
continues on its schedule and carries the BIGInfo in its **ACAD** field (the
advertising data continues to carry the BASE). A Synchronized Receiver parses the
BIGInfo from the received ACAD, emits an HCI `LE BIGInfo Advertising Report` to
the Host, and can then establish a BIG sync with `HCI_LE_BIG_Create_Sync`.

## Background

- The Rust link layer already contained `hci_le_create_big` / `hci_le_terminate_big`
  and a `BigConfig` model, but the commands were not reachable end-to-end.
- Periodic advertising sync creation/termination already worked in the C++
  controller, but periodic PDUs were only processed while the scanner was
  running, so an established sync (which the controller tracks independently of
  the scanner) lost the ability to observe the ACAD after scanning stopped.
- The receiver commands were present in the PDL but disabled in the controller.

## What changed

### Controller properties / HCI command support

- `model/controller/controller_properties.cc`
  - Added `LLFeaturesBits::ISOCHRONOUS_BROADCASTER` **and**
    `LLFeaturesBits::SYNCHRONIZED_RECEIVER` to the default `LlFeatures()`.
  - Enabled `LE_CREATE_BIG`, `LE_TERMINATE_BIG`,
    `LE_SET_PERIODIC_ADVERTISING_RECEIVE_ENABLE`, `LE_BIG_CREATE_SYNC`, and
    `LE_BIG_TERMINATE_SYNC` in the default `SupportedCommands()` mask.
  - Added `le_isochronous_broadcast_commands_` and
    `le_isochronous_synchronized_receiver_commands_` lists, applied from the
    new `features.le_isochronous_broadcast` / `features.le_isochronous_-
    synchronized_receiver` configuration toggles.
- `proto/rootcanal/configuration.proto`
  - Added `le_isochronous_broadcast` and `le_isochronous_synchronized_receiver`
    to `ControllerFeatures` (defaults to enabled).

### HCI event layout fix for Bumble interoperability

- `packets/hci_packets.pdl`
  - Changed `HCI_LE_Create_BIG_Complete`'s and `HCI_LE_BIG_Sync_Established`'s
    connection-handle prefix from `_size_(connection_handle)` (byte length) to
    `_count_(connection_handle)` (item count). The Core spec defines `Num_BIS` /
    the BIS handles as a count, and Bumble expects that count, so the events now
    parse correctly in Google Bumble.

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

### Synchronized Receiver (receiver HCI commands / BIGInfo consumption)

- `model/controller/dual_mode_controller.cc` / `.h`
  - Wired `LE_SET_PERIODIC_ADVERTISING_RECEIVE_ENABLE` to a dedicated handler and
    routed `LE_BIG_CREATE_SYNC` / `LE_BIG_TERMINATE_SYNC` via `ForwardToLl`
    (un-commenting the disabled entries).
- `model/controller/le_controller.cc` / `.h`
  - Added `LeController::LeSetPeriodicAdvertisingReceiveEnable`, which enables /
    disables the delivery of periodic advertising reports for a given sync
    (a new `receive_enabled` field on the `Synchronized` state) while keeping
    the sync itself synchronized.
  - Relaxed the periodic-PDU input gating so that an **established** periodic
    advertising sync continues to track the train and to observe the ACAD even
    after the scanner is stopped (matching real hardware behaviour).
  - Added `ParseBigInfoFromAcad`, which scans the received periodic advertising
    PDU's ACAD field for the BIGInfo AD type (`0x2C`), emits the HCI `LE BIGInfo
    Advertising Report` event to the Host, and stores the BIG info in the link
    layer keyed by (advertiser address, SID).
  - Added a `get_sync_info` controller callback exposing the advertiser address /
    SID of an established periodic sync to the link layer.
- `rust/src/llcp/iso.rs`
  - Added a `BigSyncConfig` type and a `known_big_info` map (BIGInfo received
    from a broadcaster), plus `hci_le_big_create_sync()` (validates the
    parameters, looks up the BIGInfo for the sync, allocates peripheral BIS
    handles, returns `LE Big Sync Established`) and `hci_le_big_terminate_sync()`
    (returns `LE BIG Terminate Sync Complete` + `LE Big Sync Lost`).
- `rust/src/llcp/manager.rs`
  - Routed `LeBigCreateSync` / `LeBigTerminateSync` to the ISO manager and
    exposed `store_big_info` to the FFI.
- `rust/src/ffi.rs` and `rust/include/rootcanal_rs.h`
  - Added the `get_sync_info` callback to `ControllerOps` and the
    `link_layer_le_big_info_received` C ABI used by the controller to record the
    BIGInfo received from a broadcaster.

### BIS data broadcast over the virtual air (BIS SDUs)

- `packets/link_layer_packets.pdl`
  - Filled in `LeBroadcastIsochronousPdu` (big_handle, bis_id, sequence_number,
    data) so a broadcaster can relay BIS SDUs to listening receivers.
- `model/controller/le_controller.cc` / `.h`
  - Extended `HandleIso` to recognize BIS connection handles (via
    `link_layer_get_bis_information`) and broadcast the SDU as a
    `LeBroadcastIsochronousPdu` from the train's advertising address.
  - Routed `LE_BROADCAST_ISOCHRONOUS_PDU` as a connection-less packet and added
    `IncomingLeBroadcastIsochronousPdu`, which delivers the received BIS SDU to
    the Host (via `SendIsoToHost`) only if the BIS is part of an established BIG
    sync with that broadcaster.
- `rust/src/llcp/iso.rs`, `rust/src/llcp/manager.rs`, `rust/src/ffi.rs`,
  `rust/include/rootcanal_rs.h`
  - Added `get_bis_information` and `get_bis_sync_connection_handle` (and their
    FFI exports) to look up a broadcaster's BIS and to resolve a receiver's
    synchronized BIS connection handle.

## Testing

Two Google Bumble integration tests were added and validated end-to-end against a
running RootCanal instance over a TCP HCI transport:

- `examples/python/broadcast_transmitter.py` (with `examples/python/pyproject.toml`)
  - Connects via `tcp-client:127.0.0.1:6402`.
  - Verifies the `Isochronous Broadcaster` feature and `Create/`Terminate BIG`
    support, sets up a periodic advertising train, creates a BIG with 2 BIS,
    configures the ISO data path on one BIS, and terminates the BIG.
- `examples/python/broadcast_receiver.py`
  - A two-device test (broadcaster + receiver on the same RootCanal instance).
  - Verifies the full receiver command flow: periodic advertising sync, `Set
    Periodic Advertising Receive Enable`, the `LE BIGInfo Advertising Report`
    (parsed from the ACAD), `LE BIG Create Sync` -> `LE BIG Sync Established`,
    and that an ISO SDU written by the broadcaster on a BIS is delivered to the
    receiver's Host over the virtual air (BIS broadcast).

Run them with:

```sh
bazel run //:rootcanal   # in one terminal (HCI on 6402)

cd examples/python && uv run --with bumble python broadcast_transmitter.py
cd examples/python && uv run --with bumble python broadcast_receiver.py
```

Observed output (broadcaster test):

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

Observed output (receiver test):

```
Broadcaster: BIG created, BIS: [3328, 3329]
PASS: periodic sync established (step 1) sync_handle=0x0000
PASS: Set Periodic Advertising Receive Enable status 0x00 (step 2)
PASS: BIGInfo report (step 3) num_bis=2 nse=5 iso_interval=10.0
PASS: BIG sync established (step 4): [3328, 3329]
Broadcaster: sending ISO SDU on BIS 3328
PASS: receiver got ISO SDU over the air
PASS: BIG sync terminated (step 4)
```

Supporting checks observed while developing:
- The broadcaster embeds the BIGInfo AD structure into the periodic advertising
  PDU's ACAD field on every periodic advertising event while a BIG exists.
- A separate check confirmed that `Set Periodic Advertising Receive Enable`
  (enable=0) suppresses periodic advertising **data** reports (`delta=0`) while
  the LE BIGInfo Advertising Report (from the ACAD) is still delivered, and
  reports resume when re-enabled.

## Notes / future work

- All the command flow described in the Summary is implemented and the virtual
  **air-interface BIS broadcast** works: when the broadcaster sends an ISO SDU on
  a BIS (`HCI_LE_Setup_ISO_Data_Path` + HCI ISO data), the `BIS` SDU is
  broadcast on the virtual air and the synchronized receiver delivers it to its
  Host over HCI ISO.
- `HCI_LE_BIG_Create_Sync_Test` is not implemented (future work).