#!/usr/bin/env python3
"""Pytest integration tests for LE Audio BIG/BIS behavior in RootCanal.

Start RootCanal first (default HCI TCP port 6402), then run selected tests:

    uv run --with bumble --with pytest --with pytest-asyncio \
      pytest -v examples/python/broadcast_stress_test.py
    uv run --with bumble --with pytest --with pytest-asyncio \
      pytest -v examples/python/broadcast_stress_test.py -k two_broadcasters

The sequential two-broadcaster test deliberately uses separate HCI devices and
checks that a receiver can terminate one BIG sync before syncing to a second,
different BIG handle.
"""

from __future__ import annotations

import asyncio
import os
import struct
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from bumble import device, hci
from bumble.keys import PairingKeys
from bumble.transport import open_transport

HCI_TRANSPORT = os.environ.get("ROOTCANAL_HCI", "tcp-client:127.0.0.1:6402")
BROADCASTER_IDENTITY = "F0:F1:F2:F3:F4:F5"
BROADCASTER_IRK = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
BROADCAST_CODE = bytes.fromhex("FFEEDDCCBBAA99887766554433221100")
SECOND_BROADCASTER_IDENTITY = "F6:F7:F8:F9:FA:FB"
SECOND_BROADCASTER_IRK = bytes.fromhex("102132435465768798A9BACBDCEDFE0F")
SDUS_PER_SESSION = int(os.environ.get("BROADCAST_STRESS_SDUS", "50"))
RECEIVE_TIMEOUT_SECONDS = float(os.environ.get("BROADCAST_STRESS_TIMEOUT", "5"))
NUM_BIS = int(os.environ.get("BROADCAST_STRESS_BIS", "2"))
SDU_INTERVAL_US = int(os.environ.get("BROADCAST_STRESS_SDU_INTERVAL_US", "10000"))
MAX_TRANSPORT_LATENCY_MS = int(os.environ.get("BROADCAST_STRESS_LATENCY_MS", "65"))
RTN = int(os.environ.get("BROADCAST_STRESS_RTN", "4"))
PHY = hci.PhyBit[os.environ.get("BROADCAST_STRESS_PHY", "LE_1M")]
BIS_IDS = [
    int(value)
    for value in os.environ.get("BROADCAST_STRESS_BIS_IDS", "1,2").split(",")
]
SDU_HEADER = struct.Struct("<4sIIH")
SDU_MAGIC = b"BIS!"
SDU_SIZE = 40


@dataclass
class Devices:
    broadcasters: list[device.Device]
    receivers: list[device.Device]


@pytest_asyncio.fixture
async def test_devices() -> Any:
    """Open two broadcaster and two receiver HCI transports, with cleanup."""
    async with AsyncExitStack() as stack:
        transports = [
            await stack.enter_async_context(await open_transport(HCI_TRANSPORT))
            for _ in range(4)
        ]
        broadcaster_addresses = [
            ("BIG Stress Broadcaster 1", BROADCASTER_IDENTITY, BROADCASTER_IRK),
            (
                "BIG Stress Broadcaster 2",
                SECOND_BROADCASTER_IDENTITY,
                SECOND_BROADCASTER_IRK,
            ),
        ]
        broadcasters = [
            device.Device.from_config_with_hci(
                device.DeviceConfiguration(
                    name=name,
                    address=hci.Address(address),
                    le_privacy_enabled=True,
                    irk=irk,
                ),
                *transports[index],
            )
            for index, (name, address, irk) in enumerate(broadcaster_addresses)
        ]
        receivers = [
            device.Device.from_config_with_hci(
                device.DeviceConfiguration(
                    name=f"BIG Stress Receiver {index + 1}",
                    le_privacy_enabled=True,
                    address_resolution_offload=True,
                ),
                *transports[index + 2],
            )
            for index in range(2)
        ]
        for controller in [*broadcasters, *receivers]:
            await controller.power_on()
        for controller in [*broadcasters, *receivers]:
            await controller.reset()
        for receiver in receivers:
            assert receiver.keystore is not None
            for identity, irk in (
                (BROADCASTER_IDENTITY, BROADCASTER_IRK),
                (SECOND_BROADCASTER_IDENTITY, SECOND_BROADCASTER_IRK),
            ):
                await receiver.keystore.update(
                    identity,
                    PairingKeys(
                        address_type=hci.Address.RANDOM_DEVICE_ADDRESS,
                        irk=PairingKeys.Key(irk),
                    ),
                )
            await receiver.refresh_resolving_list()
            result = await receiver.send_sync_command(
                hci.HCI_LE_Set_Address_Resolution_Enable_Command(
                    address_resolution_enable=1
                )
            )
            assert result.status == hci.HCI_SUCCESS
        yield Devices(broadcasters, receivers)
        for controller in [*receivers, *broadcasters]:
            await controller.power_off()


async def wait_until(predicate, description: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        await asyncio.sleep(0.02)


def make_sdu(session_index: int, sequence: int) -> bytes:
    header = SDU_HEADER.pack(SDU_MAGIC, session_index, sequence, SDU_SIZE)
    seed = (session_index * 37 + sequence * 13) & 0xFF
    body = bytes((seed + offset) & 0xFF for offset in range(SDU_SIZE - len(header)))
    return header + body


async def run_broadcast(
    broadcaster: device.Device,
    receivers: list[device.Device],
    session_index: int,
    *,
    encrypted: bool = False,
    advertiser_identity: str = BROADCASTER_IDENTITY,
    bis_ids: list[int] | None = None,
    sdu_count: int | None = None,
    terminate_receiver_syncs: bool = True,
) -> dict[str, dict[int, set[int]]]:
    selected_bis = BIS_IDS if bis_ids is None else bis_ids
    count = SDUS_PER_SESSION if sdu_count is None else sdu_count
    if not selected_bis or len(set(selected_bis)) != len(selected_bis):
        raise ValueError("BIS IDs must be a non-empty unique list")
    if any(bis < 1 or bis > NUM_BIS for bis in selected_bis):
        raise ValueError(f"BIS IDs must be in the range 1..{NUM_BIS}")
    expected = {sequence: make_sdu(session_index, sequence) for sequence in range(count)}
    sid = session_index & 0x0F
    if not await broadcaster.update_rpa():
        raise RuntimeError("Could not rotate broadcaster RPA")
    rpa = broadcaster.random_address
    assert rpa is not None

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
        random_address=rpa,
        periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
            periodic_advertising_interval_min=100,
            periodic_advertising_interval_max=100,
        ),
        auto_restart=False,
        auto_start=True,
    )
    big = None
    allocated_big_handle: int | None = None
    receiver_syncs: list[tuple[Any, Any]] = []
    completed_big_syncs: list[Any] = []
    try:
        await advertising_set.start_periodic()
        # Bumble allocates BIG handles sequentially per device. A second device
        # normally also starts at handle 0, so reserve lower handles when a test
        # needs to deliberately construct a distinct-handle case.
        reserved = getattr(broadcaster, "_rootcanal_test_big_handle_offset", 0)
        for handle in range(reserved):
            broadcaster.bigs[handle] = None
        big = await broadcaster.create_big(
            advertising_set,
            parameters=device.BigParameters(
                num_bis=NUM_BIS,
                sdu_interval=SDU_INTERVAL_US,
                max_sdu=SDU_SIZE,
                max_transport_latency=MAX_TRANSPORT_LATENCY_MS,
                rtn=RTN,
                phy=PHY,
                broadcast_code=BROADCAST_CODE if encrypted else None,
            ),
        )
        assert len(big.bis_links) == NUM_BIS
        allocated_big_handle = big.big_handle
        broadcaster._rootcanal_test_last_big_handle = big.big_handle

        received: dict[str, dict[int, set[int]]] = {
            receiver.name: {bis_id: set() for bis_id in selected_bis}
            for receiver in receivers
        }
        errors: list[str] = []
        receive_queue: asyncio.Queue[tuple[str, int, int]] = asyncio.Queue()
        for receiver in receivers:
            if not hasattr(receiver, "_rootcanal_test_periodic_syncs"):
                receiver._rootcanal_test_periodic_syncs = []
            sync = await receiver.create_periodic_advertising_sync(
                advertiser_address=hci.Address(advertiser_identity),
                sid=sid,
                sync_timeout=10.0,
            )
            await wait_until(
                lambda sync=sync: sync.state == sync.State.ESTABLISHED,
                f"periodic sync for {receiver.name}",
            )
            biginfo_future = asyncio.get_running_loop().create_future()

            def on_biginfo(advertisement, future=biginfo_future) -> None:
                if not future.done():
                    future.set_result(advertisement)

            sync.on("biginfo_advertisement", on_biginfo)
            response = await receiver.send_sync_command(
                hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(
                    sync_handle=sync.sync_handle, enable=1
                )
            )
            assert response.status == hci.HCI_SUCCESS
            biginfo = await asyncio.wait_for(biginfo_future, timeout=10.0)
            assert biginfo.num_bis == NUM_BIS
            assert bool(biginfo.encryption) is encrypted
            big_sync = await receiver.create_big_sync(
                sync,
                parameters=device.BigSyncParameters(
                    big_sync_timeout=4000,
                    bis=selected_bis,
                    broadcast_code=BROADCAST_CODE if encrypted else None,
                ),
            )
            receiver_syncs.append((sync, big_sync))
            completed_big_syncs.append(big_sync)
            receiver._rootcanal_test_periodic_syncs.append(sync)
            assert len(big_sync.bis_links) == len(selected_bis)

            for bis_id, rx_link in zip(selected_bis, big_sync.bis_links, strict=True):
                def make_sink(receiver_name: str, selected_id: int):
                    def on_iso(packet) -> None:
                        payload = bytes(packet.iso_sdu_fragment)
                        if len(payload) != SDU_SIZE:
                            errors.append(f"{receiver_name} BIS {selected_id}: bad SDU length")
                            return
                        try:
                            magic, packet_session, sequence, embedded_size = SDU_HEADER.unpack_from(payload)
                        except struct.error as error:
                            errors.append(f"{receiver_name} BIS {selected_id}: {error}")
                            return
                        if (
                            magic != SDU_MAGIC
                            or packet_session != session_index
                            or embedded_size != SDU_SIZE
                            or sequence not in expected
                            or payload != expected.get(sequence)
                        ):
                            errors.append(f"{receiver_name} BIS {selected_id}: corrupt SDU")
                            return
                        stream = received[receiver_name][selected_id]
                        if sequence in stream:
                            errors.append(f"{receiver_name} BIS {selected_id}: duplicate SDU")
                            return
                        stream.add(sequence)
                        receive_queue.put_nowait((receiver_name, selected_id, sequence))

                    return on_iso

                rx_link.sink = make_sink(receiver.name, bis_id)

        for bis_id in selected_bis:
            await big.bis_links[bis_id - 1].setup_data_path(
                device.BisLink.Direction.HOST_TO_CONTROLLER
            )
        for start in range(0, count, 5):
            for sequence in range(start, min(start + 5, count)):
                for bis_id in selected_bis:
                    big.bis_links[bis_id - 1].write(expected[sequence])
            await asyncio.sleep(0.03)

        total = count * len(selected_bis) * len(receivers)
        deadline = asyncio.get_running_loop().time() + RECEIVE_TIMEOUT_SECONDS
        for _ in range(total):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"Received fewer than {total} broadcast SDUs")
            await asyncio.wait_for(receive_queue.get(), timeout=remaining)
        assert not errors, "; ".join(errors)
        for receiver_name, streams in received.items():
            for bis_id, sequences in streams.items():
                assert sequences == set(expected), (
                    f"{receiver_name} BIS {bis_id}: {len(sequences)}/{count} SDUs"
                )
        receiver._rootcanal_test_last_big_syncs = completed_big_syncs
        return received
    finally:
        if terminate_receiver_syncs:
            for sync, big_sync in reversed(receiver_syncs):
                if big_sync.state == device.BigSync.State.ACTIVE:
                    await big_sync.terminate()
                await sync.terminate()
                if sync in receiver._rootcanal_test_periodic_syncs:
                    receiver._rootcanal_test_periodic_syncs.remove(sync)
        else:
            # Keep the receiver sync active for the caller to terminate between
            # sequential broadcaster attempts.
            receiver._rootcanal_test_last_big_syncs = completed_big_syncs
        if big is not None and big.state == device.Big.State.ACTIVE:
            await big.terminate()
        if allocated_big_handle is not None:
            broadcaster._rootcanal_test_last_big_handle = allocated_big_handle
        if advertising_set.enabled:
            await advertising_set.stop_periodic()
            await advertising_set.stop()
        await advertising_set.remove()


@pytest.mark.asyncio
async def test_unencrypted_broadcast(test_devices: Devices) -> None:
    await run_broadcast(test_devices.broadcasters[0], test_devices.receivers[:1], 0)


@pytest.mark.asyncio
async def test_encrypted_broadcast(test_devices: Devices) -> None:
    await run_broadcast(
        test_devices.broadcasters[0], test_devices.receivers[:1], 1, encrypted=True
    )


@pytest.mark.asyncio
async def test_selected_bis_delivery(test_devices: Devices) -> None:
    if NUM_BIS < 3:
        pytest.skip("Set BROADCAST_STRESS_BIS=3 or greater to test a non-default BIS ID")
    await run_broadcast(
        test_devices.broadcasters[0], test_devices.receivers[:1], 2, bis_ids=[NUM_BIS]
    )


@pytest.mark.asyncio
async def test_two_receivers_receive_identical_broadcast(test_devices: Devices) -> None:
    results = await run_broadcast(
        test_devices.broadcasters[0], test_devices.receivers, 3,
        sdu_count=min(SDUS_PER_SESSION, 50),
    )
    first, second = test_devices.receivers
    assert results[first.name] == results[second.name]


@pytest.mark.asyncio
async def test_repeated_encryption_and_handle_reuse(test_devices: Devices) -> None:
    for session_index in range(4):
        await run_broadcast(
            test_devices.broadcasters[0],
            test_devices.receivers[:1],
            session_index + 10,
            encrypted=bool(session_index % 2),
        )


async def run_two_broadcaster_sequential_syncs(
    test_devices: Devices,
    *,
    first_big_handle: int,
    second_big_handle: int,
    session_base: int,
) -> None:
    """Sync to separate broadcasters sequentially, terminating between."""
    receiver = test_devices.receivers[0]
    first, second = test_devices.broadcasters
    first_stream = await run_broadcast(
        first,
        [receiver],
        session_base,
        advertiser_identity=BROADCASTER_IDENTITY,
        sdu_count=min(SDUS_PER_SESSION, 20),
        terminate_receiver_syncs=False,
    )
    # Terminating the first broadcaster's BIG must not leave a receiver sync or
    # BIG handle allocated; it also ensures the second attempt is sequential.
    # run_broadcast leaves its periodic/BIG sync alive for this test to stop it.
    # The active sync is tracked on Bumble's receiver device.
    active_syncs = receiver._rootcanal_test_last_big_syncs
    assert active_syncs, "First broadcaster did not establish receiver BIG sync"
    old_big_handles = {big_sync.big_handle for big_sync in active_syncs}
    first_broadcaster_big_handle = first._rootcanal_test_last_big_handle
    for big_sync in active_syncs:
        if big_sync.state == device.BigSync.State.ACTIVE:
            await big_sync.terminate()
    receiver._rootcanal_test_last_terminated_big_handles = old_big_handles
    for sync in list(receiver._rootcanal_test_periodic_syncs):
        await sync.terminate()
    receiver._rootcanal_test_periodic_syncs.clear()
    await asyncio.sleep(0.1)

    if first_big_handle != second_big_handle:
        second._rootcanal_test_big_handle_offset = second_big_handle
    second_stream = await run_broadcast(
        second,
        [receiver],
        session_base + 1,
        advertiser_identity=SECOND_BROADCASTER_IDENTITY,
        sdu_count=min(SDUS_PER_SESSION, 20),
    )
    second_broadcaster_big_handle = second._rootcanal_test_last_big_handle
    assert second_stream
    new_big_syncs = receiver._rootcanal_test_last_big_syncs
    assert len(new_big_syncs) == 1, "Second BIG sync was not established"
    new_big_handle = new_big_syncs[0].big_handle
    if first_big_handle != second_big_handle:
        assert first_broadcaster_big_handle != second_broadcaster_big_handle, (
            "The two broadcaster devices did not allocate distinct BIG handles: "
            f"{first_broadcaster_big_handle}"
        )
    else:
        assert first_broadcaster_big_handle == second_broadcaster_big_handle, (
            "The broadcasters were expected to reuse a BIG handle, got "
            f"{first_broadcaster_big_handle} and {second_broadcaster_big_handle}"
        )
    # Bumble assigns receiver-side sync handles locally and they need not match
    # a broadcaster's BIG handle. Payload delivery above validates RootCanal's
    # association of the periodic train with its broadcaster BIG handle.
    if first_big_handle != second_big_handle:
        assert old_big_handles != {new_big_handle}
    else:
        assert old_big_handles == {next(iter(old_big_handles))}
        assert new_big_syncs[0] not in active_syncs, (
            "Second broadcaster reused stale receiver BIG-sync object"
        )
    assert first_stream


@pytest.mark.asyncio
async def test_two_broadcasters_sequential_big_sync(test_devices: Devices) -> None:
    """Sync to distinct BIG handles in sequence, terminating between."""
    await run_two_broadcaster_sequential_syncs(
        test_devices,
        first_big_handle=0,
        second_big_handle=1,
        session_base=5,
    )


@pytest.mark.asyncio
async def test_two_broadcasters_same_big_handle_sequential_big_sync(
    test_devices: Devices,
) -> None:
    """Sync to two broadcasters reusing one BIG handle, with teardown between."""
    await run_two_broadcaster_sequential_syncs(
        test_devices,
        first_big_handle=0,
        second_big_handle=0,
        session_base=7,
    )
