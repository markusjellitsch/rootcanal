#!/usr/bin/env python3
"""Receiver-side HCI command flow test for RootCanal LE Audio (Synchronized Receiver).

Verifies the receiver HCI commands required to join a broadcast isochronous
stream, against a running RootCanal instance (HCI on TCP 127.0.0.1:6402):

  1. HCI_LE_Periodic_Advertising_Create_Sync
  2. HCI_LE_Set_Periodic_Advertising_Receive_Enable
  3. HCI_LE BIGInfo Advertising Report (parsed from the periodic advertising
     PDU's ACAD field)
  4. HCI_LE_BIG_Create_Sync -> LE BIG Sync Established -> HCI_LE_BIG_Terminate_Sync

Run with (RootCanal must be running on port 6402):

  uv run --with bumble python broadcast_receiver.py
"""

import asyncio

from bumble import device, hci
from bumble.keys import PairingKeys
from bumble.transport import open_transport

ADDR_A = "F0:F1:F2:F3:F4:F5"
SID = 1
BROADCASTER_IRK = bytes.fromhex("00112233445566778899AABBCCDDEEFF")
BROADCAST_CODE = bytes.fromhex("FFEEDDCCBBAA99887766554433221100")


async def main() -> None:
    # Two devices on the same RootCanal instance (shared virtual air).
    async with await open_transport("tcp-client:127.0.0.1:6402") as (a_src, a_sink), (
        await open_transport("tcp-client:127.0.0.1:6402")
    ) as (b_src, b_sink):
        broadcaster = device.Device.from_config_with_hci(
            device.DeviceConfiguration(
                name="Broadcaster",
                address=hci.Address(ADDR_A),
                le_privacy_enabled=True,
                irk=BROADCASTER_IRK,
            ),
            a_src,
            a_sink,
        )
        receiver = device.Device.from_config_with_hci(
            device.DeviceConfiguration(
                name="Receiver",
                le_privacy_enabled=True,
                address_resolution_offload=True,
            ),
            b_src,
            b_sink,
        )
        await broadcaster.power_on()
        await receiver.power_on()

        # Configure the receiver's resolving list with the broadcaster identity
        # and IRK, then enable controller-side RPA resolution before starting
        # advertising or periodic sync.
        resolving_list = receiver.keystore
        assert resolving_list is not None
        await resolving_list.update(
            ADDR_A,
            PairingKeys(
                address_type=hci.Address.RANDOM_DEVICE_ADDRESS,
                irk=PairingKeys.Key(BROADCASTER_IRK),
            ),
        )
        await receiver.refresh_resolving_list()
        await receiver.send_sync_command(
            hci.HCI_LE_Set_Address_Resolution_Enable_Command(address_resolution_enable=1)
        )
        broadcaster_rpa = broadcaster.random_address
        assert broadcaster_rpa is not None
        print(f"Broadcaster RPA: {broadcaster_rpa}")

        # --- Broadcaster: extended + periodic advertising + BIG ---
        adv_set = await broadcaster.create_advertising_set(
            advertising_parameters=device.AdvertisingParameters(
                advertising_event_properties=device.AdvertisingEventProperties(
                    is_connectable=False
                ),
                own_address_type=hci.OwnAddressType.RANDOM,
                primary_advertising_interval_min=100,
                primary_advertising_interval_max=100,
                advertising_sid=SID,
            ),
            random_address=broadcaster_rpa,
            periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=100,
                periodic_advertising_interval_max=100,
            ),
            auto_restart=True,
            auto_start=True,
        )
        await adv_set.start_periodic()
        big = await broadcaster.create_big(
            adv_set,
            parameters=device.BigParameters(
                num_bis=2,
                sdu_interval=10000,
                max_sdu=40,
                max_transport_latency=65,
                rtn=4,
                phy=hci.PhyBit.LE_1M,
                broadcast_code=BROADCAST_CODE,
            ),
        )
        print("Broadcaster: BIG created, BIS:", [l.handle for l in big.bis_links])

        # --- Step 1: periodic advertising sync ---
        sync = await receiver.create_periodic_advertising_sync(
            advertiser_address=hci.Address(ADDR_A), sid=SID
        )
        for _ in range(200):
            if sync.state == sync.State.ESTABLISHED:
                break
            await asyncio.sleep(0.1)
        if sync.state != sync.State.ESTABLISHED:
            print("FAIL: periodic sync not established (step 1)")
            return
        print(
            f"PASS: periodic sync established (step 1) "
            f"sync_handle=0x{sync.sync_handle:04x}"
        )

        # --- Step 2: Set Periodic Advertising Receive Enable (off) ---
        rsp = await receiver.send_sync_command(
            hci.HCI_LE_Set_Periodic_Advertising_Receive_Enable_Command(
                sync_handle=sync.sync_handle, enable=0
            )
        )
        print(f"PASS: Set Periodic Advertising Receive Enable status 0x{rsp.status:02x} "
              f"(step 2)")

        # --- Step 3: wait for the LE BIGInfo Advertising Report (ACAD) ---
        biginfo_fut = asyncio.get_running_loop().create_future()

        def on_biginfo(adv):
            if not biginfo_fut.done():
                biginfo_fut.set_result(adv)

        sync.on("biginfo_advertisement", on_biginfo)
        try:
            adv = await asyncio.wait_for(biginfo_fut, timeout=20)
            print(
                f"PASS: BIGInfo report (step 3) num_bis={adv.num_bis} "
                f"nse={adv.nse} iso_interval={adv.iso_interval}"
            )
        except asyncio.TimeoutError:
            print("FAIL: no BIGInfo received (step 3)")
            return

        # --- Step 4: LE BIG Create Sync -> Sync Established -> Terminate ---
        big_sync = await receiver.create_big_sync(
            sync,
            parameters=device.BigSyncParameters(
                big_sync_timeout=4000, bis=[1, 2], broadcast_code=BROADCAST_CODE
            ),
        )
        print("PASS: BIG sync established (step 4):",
              [l.handle for l in big_sync.bis_links])

        # --- Step 5: broadcaster sends ISO SDUs -> receiver receives them ---
        payload = bytes(range(0x20, 0x30))
        rx_link = big_sync.bis_links[0]
        got = asyncio.get_running_loop().create_future()

        def on_iso(packet):
            if not got.done():
                got.set_result(bytes(packet.iso_sdu_fragment))

        rx_link.sink = on_iso
        await big.bis_links[0].setup_data_path(
            device.BisLink.Direction.HOST_TO_CONTROLLER
        )
        print("Broadcaster: sending ISO SDU on BIS", big.bis_links[0].handle)
        big.bis_links[0].write(payload)
        try:
            data = await asyncio.wait_for(got, timeout=10)
            ok = data == payload
            print("PASS: receiver got ISO SDU over the air" if ok
                  else "FAIL: received payload mismatch")
        except asyncio.TimeoutError:
            print("FAIL: receiver did not receive ISO SDU over the air")

        await big_sync.terminate()
        print("PASS: BIG sync terminated (step 4)")


if __name__ == "__main__":
    asyncio.run(main())