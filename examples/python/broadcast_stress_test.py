#!/usr/bin/env python3
"""Repeated LE Audio BIG/BIS transmit/receive stress test for RootCanal.

Runs repeated broadcast sessions over one RootCanal instance, alternating
unencrypted and encrypted BIGs. Each session creates a fresh periodic sync and
BIG sync, sends many uniquely numbered BIS SDUs, verifies every SDU at the
receiver, and tears down both syncs before continuing.

Start RootCanal first (default HCI TCP port 6402), then run:

    uv run --with bumble python examples/python/broadcast_stress_test.py

Optional settings:

    BROADCAST_STRESS_SESSIONS=20 BROADCAST_STRESS_SDUS=100 \
      uv run --with bumble python examples/python/broadcast_stress_test.py

This tests controller/HCI behavior and virtual-air packet handling; it does not
measure real-time scheduling or audio quality.
"""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import time

from bumble import device, hci
from bumble.keys import PairingKeys
from bumble.transport import open_transport

# --- Monkey-patch: bumble BIG-sync handle leak workaround -----------------------
#
# bumble's BigSync.terminate() sends HCI_LE_BIG_Terminate_Sync and waits for the
# Command Complete, but never removes the sync from device.big_syncs. bumble only
# pops a sync handle in on_big_sync_lost() (triggered by the LE_BIG_Sync_Lost
# event) and has no handler for HCI_LE_BIG_Terminate_Sync_Complete, so a clean
# Host-initiated termination leaks the handle.
#
# RootCanal correctly releases the BigSync config on terminating the sync AND is
# spec-correct in NOT emitting LE_BIG_Sync_Lost for a clean Host-initiated
# termination, so from the controller's perspective the handle is free. On bumble
# however device.next_big_handle() skips the leaked present in big_syncs, so the
# receiver's BIG handle drifts +1 from the broadcaster's on every session.
# get_bis_sync_connection_handle() then looks up big_sync_config[receiver_handle]
# while the broadcast PDU carries the broadcaster's (lower) handle, and every BIS
# SDU is dropped as "unsynchronized".
#
# Fix: mirror bumble's own on_big_sync_lost cleanup so the receiver releases its
# sync handle on termination, keeping it aligned with the broadcaster.
_orig_big_sync_terminate = device.BigSync.terminate


async def _patched_big_sync_terminate(self: "device.BigSync") -> None:
    if self.state != device.BigSync.State.ACTIVE:
        await _orig_big_sync_terminate(self)
        return
    # Release the sync handle + its BIS links (same bookkeeping as on_big_sync_lost).
    self.device.big_syncs.pop(self.big_handle, None)
    for bis_link in self.bis_links:
        self.device.bis_links.pop(bis_link.handle, None)
    await _orig_big_sync_terminate(self)


device.BigSync.terminate = _patched_big_sync_terminate
# --------------------------------------------------------------------------------


HCI_TRANSPORT = os.environ.get("ROOTCANAL_HCI", "tcp-client:127.0.0.1:6402")
BROADCASTER_IDENTITY = "F0:F1:F2:F3:F4:F5"
ADVERTISING_SID = 1
BROADCASTER_IRK = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
BROADCAST_CODE = bytes.fromhex("FFEEDDCCBBAA99887766554433221100")
NUM_SESSIONS = int(os.environ.get("BROADCAST_STRESS_SESSIONS", "10"))
SDUS_PER_SESSION = int(os.environ.get("BROADCAST_STRESS_SDUS", "50"))
RECEIVE_TIMEOUT_SECONDS = float(os.environ.get("BROADCAST_STRESS_TIMEOUT", "5"))

# Header plus test marker. The marker detects accidental acceptance of stale or
# duplicated packets from an earlier BIG/session.
SDU_HEADER = struct.Struct("<4sIIH")
SDU_MAGIC = b"BIS!"
SDU_SIZE = 40


async def wait_until(predicate, description: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        await asyncio.sleep(0.02)


def make_sdu(session_index: int, sequence: int) -> bytes:
    header = SDU_HEADER.pack(SDU_MAGIC, session_index, sequence, SDU_SIZE)
    # Payload is deterministic and varies for each packet.
    seed = (session_index * 37 + sequence * 13) & 0xFF
    body = bytes((seed + offset) & 0xFF for offset in range(SDU_SIZE - len(header)))
    return header + body


async def run_session(
    broadcaster: device.Device,
    receiver: device.Device,
    session_index: int,
    encrypted: bool,
) -> tuple[int, int]:
    expected = {sequence: make_sdu(session_index, sequence) for sequence in range(SDUS_PER_SESSION)}
    received: set[int] = set()
    phase = "rotate RPA"
    errors: list[str] = []
    receive_queue: asyncio.Queue[bytes] = asyncio.Queue()

    # Make every advertising SID distinct across sessions, while remaining in
    # the valid 0..15 range. Rotate the RPA before each advertising set starts.
    sid = (ADVERTISING_SID + session_index) & 0x0F
    if not await broadcaster.update_rpa():
        raise RuntimeError("Could not rotate broadcaster RPA before stress session")
    broadcaster_rpa = broadcaster.random_address
    assert broadcaster_rpa is not None

    phase = "create advertising set"
    advertising_set = await broadcaster.create_advertising_set(
        advertising_parameters=device.AdvertisingParameters(
            advertising_event_properties=device.AdvertisingEventProperties(
                is_connectable=False,
            ),
            own_address_type=hci.OwnAddressType.RANDOM,
            primary_advertising_interval_min=100,
            primary_advertising_interval_max=100,
            advertising_sid=sid,
        ),
        random_address=broadcaster_rpa,
        periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=100,
            periodic_advertising_interval_max=100,
        ),
        auto_restart=False,
        auto_start=True,
    )

    broadcast_code = BROADCAST_CODE if encrypted else None
    big = None
    periodic_sync = None
    big_sync = None
    try:
        phase = "start periodic advertising"
        await advertising_set.start_periodic()
        phase = "create BIG"
        big = await broadcaster.create_big(
            advertising_set,
            parameters=device.BigParameters(
                num_bis=2,
                sdu_interval=10_000,
                max_sdu=SDU_SIZE,
                max_transport_latency=65,
                rtn=4,
                phy=hci.PhyBit.LE_1M,
                broadcast_code=broadcast_code,
            ),
        )

        phase = "create periodic advertising sync"
        periodic_sync = await receiver.create_periodic_advertising_sync(
            advertiser_address=hci.Address(BROADCASTER_IDENTITY),
            sid=sid,
            sync_timeout=10.0,
        )
        phase = "wait for periodic sync establishment"
        await wait_until(
            lambda: periodic_sync.state == periodic_sync.State.ESTABLISHED,
            f"periodic sync, session {session_index}",
        )

        phase = "wait for BIGInfo"
        biginfo_future = asyncio.get_running_loop().create_future()

        def on_biginfo(advertisement) -> None:
            if not biginfo_future.done():
                biginfo_future.set_result(advertisement)

        periodic_sync.on("biginfo_advertisement", on_biginfo)

        # Explicitly enable periodic reports for this sync. BIGInfo reporting is
        # separately event-masked, but enabling reports keeps this test aligned
        # with the conventional receiver setup.
        phase = "enable periodic advertising receive"
        receive_enable = await receiver.send_sync_command(
            hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(
                sync_handle=periodic_sync.sync_handle,
                enable=1,
            )
        )
        if receive_enable.status != hci.HCI_SUCCESS:
            raise RuntimeError(
                f"Set Periodic Advertising Receive Enable failed: 0x{receive_enable.status:02X}"
            )

        try:
            biginfo = await asyncio.wait_for(biginfo_future, timeout=10.0)
        except asyncio.TimeoutError as error:
            raise TimeoutError(f"No BIGInfo report in session {session_index}") from error
        if biginfo.num_bis != 2:
            raise AssertionError(f"Expected Num_BIS=2, got {biginfo.num_bis}")
        if bool(biginfo.encryption) != encrypted:
            raise AssertionError(
                f"BIGInfo encryption={biginfo.encryption}, expected {encrypted}"
            )

        phase = "create BIG sync"
        big_sync = await receiver.create_big_sync(
            periodic_sync,
            parameters=device.BigSyncParameters(
                big_sync_timeout=4000,
                bis=[1, 2],
                broadcast_code=broadcast_code,
            ),
        )
        if len(big_sync.bis_links) != 2:
            raise AssertionError(f"Expected two synchronized BIS links, got {len(big_sync.bis_links)}")

        phase = "configure BIS receive and transmit paths"
        rx_link = big_sync.bis_links[0]

        def on_iso(packet) -> None:
            payload = bytes(packet.iso_sdu_fragment)
            if len(payload) != SDU_SIZE:
                errors.append(f"Received SDU length {len(payload)}, expected {SDU_SIZE}")
                return
            try:
                magic, packet_session, sequence, embedded_size = SDU_HEADER.unpack_from(payload)
            except struct.error as error:
                errors.append(f"Malformed SDU header: {error}")
                return
            if magic != SDU_MAGIC or packet_session != session_index or embedded_size != SDU_SIZE:
                errors.append(
                    f"Unexpected SDU header: magic={magic!r} session={packet_session} "
                    f"size={embedded_size}"
                )
                return
            if sequence not in expected:
                errors.append(f"Received unexpected sequence {sequence}")
                return
            if payload != expected[sequence]:
                errors.append(f"Payload mismatch for sequence {sequence}")
                return
            if sequence in received:
                errors.append(f"Duplicate SDU sequence {sequence}")
                return
            received.add(sequence)
            receive_queue.put_nowait(payload)

        rx_link.sink = on_iso
        tx_link = big.bis_links[0]
        await tx_link.setup_data_path(device.BisLink.Direction.HOST_TO_CONTROLLER)

        # Burst SDUs in small batches. Yield between writes so HCI command and
        # event processing can progress, while still exercising queueing and
        # repeated packet delivery more aggressively than a single-SDU example.
        batch_size = 5
        phase = "transmit/receive BIS SDUs"
        for start in range(0, SDUS_PER_SESSION, batch_size):
            for sequence in range(start, min(start + batch_size, SDUS_PER_SESSION)):
                tx_link.write(expected[sequence])
            await asyncio.sleep(0.03)

        deadline = asyncio.get_running_loop().time() + RECEIVE_TIMEOUT_SECONDS
        while len(received) < SDUS_PER_SESSION:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                missing = sorted(set(expected) - received)
                raise TimeoutError(
                    f"Session {session_index}: received {len(received)}/{SDUS_PER_SESSION}; "
                    f"missing {missing[:20]}"
                )
            await asyncio.wait_for(receive_queue.get(), timeout=remaining)

        if errors:
            raise AssertionError("; ".join(errors))

        return len(received), len(errors)
    except Exception as error:
        raise RuntimeError(f"session {session_index} failed during {phase}: {error!r}") from error
    finally:
        if big_sync is not None and big_sync.state == device.BigSync.State.ACTIVE:
            await big_sync.terminate()
        if periodic_sync is not None:
            await periodic_sync.terminate()
        if big is not None and big.state == device.Big.State.ACTIVE:
            await big.terminate()
        if advertising_set.enabled:
            await advertising_set.stop_periodic()
            await advertising_set.stop()
        await advertising_set.remove()


async def main() -> None:
    if NUM_SESSIONS < 1 or SDUS_PER_SESSION < 1:
        raise ValueError("BROADCAST_STRESS_SESSIONS and BROADCAST_STRESS_SDUS must be positive")
    if SDU_SIZE > 0x0FFF:
        raise ValueError("SDU_SIZE must fit the HCI/ISO Max_SDU limit")

    async with await open_transport(HCI_TRANSPORT) as (tx_source, tx_sink), (
        await open_transport(HCI_TRANSPORT)
    ) as (rx_source, rx_sink):
        broadcaster = device.Device.from_config_with_hci(
            device.DeviceConfiguration(
                name="BIG Stress Broadcaster",
                address=hci.Address(BROADCASTER_IDENTITY),
                le_privacy_enabled=True,
                irk=BROADCASTER_IRK,
            ),
            tx_source,
            tx_sink,
        )
        receiver = device.Device.from_config_with_hci(
            device.DeviceConfiguration(
                name="BIG Stress Receiver",
                le_privacy_enabled=True,
                address_resolution_offload=True,
            ),
            rx_source,
            rx_sink,
        )
        await broadcaster.power_on()
        await receiver.power_on()
        # Controller reset clears Rootcanal's ISO manager state as well as HCI
        # handles. Reset both endpoints before the stress loop so its BIG
        # handle reuse exercises normal termination, not device-reset cleanup.
        await broadcaster.reset()
        await receiver.reset()

        # Put the broadcaster identity/IRK in the receiver resolving list so
        # periodic sync and subsequent BIS packets from its RPA resolve alike.
        assert receiver.keystore is not None
        await receiver.keystore.update(
            BROADCASTER_IDENTITY,
            PairingKeys(
                address_type=hci.Address.RANDOM_DEVICE_ADDRESS,
                irk=PairingKeys.Key(BROADCASTER_IRK),
            ),
        )
        await receiver.refresh_resolving_list()
        resolution = await receiver.send_sync_command(
            hci.HCI_LE_Set_Address_Resolution_Enable_Command(address_resolution_enable=1)
        )
        if resolution.status != hci.HCI_SUCCESS:
            raise RuntimeError(f"Enabling address resolution failed: 0x{resolution.status:02X}")

        passed_sdu_count = 0
        failures: list[str] = []
        start_time = time.monotonic()
        for session_index in range(NUM_SESSIONS):
            encrypted = session_index % 2 == 1
            label = "encrypted" if encrypted else "unencrypted"
            print(
                f"Session {session_index + 1}/{NUM_SESSIONS}: {label}, "
                f"{SDUS_PER_SESSION} SDUs",
                flush=True,
            )
            try:
                received, _ = await run_session(
                    broadcaster, receiver, session_index, encrypted
                )
                passed_sdu_count += received
                print(f"  PASS: received {received}/{SDUS_PER_SESSION} SDUs", flush=True)
            except Exception as error:  # keep later sessions diagnostic after a failure
                failures.append(f"session {session_index} ({label}): {error!r}")
                print(f"  FAIL: {error!r}", file=sys.stderr, flush=True)
                break

        elapsed = time.monotonic() - start_time
        print(
            f"Completed {NUM_SESSIONS} sessions, {passed_sdu_count}/"
            f"{NUM_SESSIONS * SDUS_PER_SESSION} SDUs verified in {elapsed:.1f}s",
            flush=True,
        )
        if failures:
            raise RuntimeError("Stress test failures:\n" + "\n".join(failures))
        await broadcaster.power_off()
        await receiver.power_off()


if __name__ == "__main__":
    asyncio.run(main())
