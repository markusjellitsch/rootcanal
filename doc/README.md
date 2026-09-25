# RootCanal internal architecture charts

This directory contains PlantUML sequence diagrams describing how RootCanal
works internally, based on a read of the actual source code.

## Files

| File | Topic |
|------|-------|
| `index.html` | Rendered diagram gallery (open in a browser) |
| `cpp_to_rust_ffi.puml` | How C++ controllers call into the Rust link manager / link layer modules, and how the Rust modules call back into C++ via `ControllerOps`. Covers both the **outer** C ABI (`ffi.h`/`ffi.cc` → `rust/src/ffi.rs`) and the **inner** callbacks (`ControllerOps` struct). |
| `controller_to_controller.puml` | How two virtual controller instances exchange BLE Link-Layer (LL) packets with each other through the `PhyLayer`, including how a sniffing device attached to the same PHY observes the exchange. |
| `ble_ll_tracing_wireshark.puml` | How to enable and consume PCAP traces produced by RootCanal, and how to use Wireshark to inspect BLE LL PDUs (and their HCI counterpart). |
| `*.svg` / `*.png` | Rendered outputs of each diagram. |

## Render to PNG / SVG

```sh
plantuml -tpng -tsvg charts/cpp_to_rust_ffi.puml \
                 charts/controller_to_controller.puml \
                 charts/ble_ll_tracing_wireshark.puml
```

or, to render everything as PNG + SVG:

```sh
plantuml -tpng -tsvg charts/*.puml
```

You can also paste the `.puml` sources into <https://www.plantuml.com/plantuml> to
view them in a browser.

The fastest way to browse them is to open **`charts/index.html`** in a browser — it
embeds all three rendered diagrams with explanations.

You can also paste the `.puml` sources into <https://www.plantuml.com/plantuml> to
view them in a browser.

## Short architecture summary (context for the diagrams)

RootCanal is a virtual Bluetooth Controller. A running rootcanal process exposes
four TCP ports:

- **HCI channel** (`--hci_port`, default 6402): each new TCP connection spawns a
  new virtual controller (a `HciDevice`, which is a `DualModeController`,
  composed of a BR/EDR controller and an LE controller).
- **Test channel** (`--test_port`, default 6401): control commands
  (create PHYs, add devices, attach devices to PHYs, timers, ...).
- **BR/EDR Phy channel** (`--link_port`, default 6403): raw serialized link-layer
  packets (BR/EDR) bridged to the in-process PHY.
- **LE Phy channel** (`--link_ble_port`, default 6404): raw serialized link-layer
  packets (LE) bridged to the in-process PHY.

All controllers live in one process, in one `TestModel`. Communication between
controller instances is purely in-memory through virtual `PhyLayer`s (which
simulate the air). The **Rust modules** never talk to the outside world directly —
they are embedded in the C++ controllers via a C ABI FFI bridge.

### The two FFI layers

1. **Outer C ABI** — the public entry points the C++ (`DualModeController`)
   calls into the Rust static library:
   - `ffi_controller_new / receive_hci / receive_ll / tick / delete` and
     `ffi_generate_rpa` live in `model/controller/ffi.h/.cc`. These are the
     "live" FFI used for the whole controller framework.
   - `link_manager_*` and `link_layer_*` functions in `rust/src/ffi.rs`
     (`#[no_mangle] pub extern "C"`) are the bridge used by the newer
     Rust-based BR/EDR Link Manager (`lmp`) and LE Link Layer (`llcp`)
     state machines.

2. **Inner `ControllerOps` callbacks** — a `#[repr(C)]` struct of C function
   pointers that the Rust modules call back into C++ (`user_pointer` is the
   `BrEdrController*`/`LeController*`). These let Rust request HCI events to be
   sent to the host, serialize LLCP/LMP packets onto the air, and query
   connection/feature state held by C++.

> More detail in each diagram file.