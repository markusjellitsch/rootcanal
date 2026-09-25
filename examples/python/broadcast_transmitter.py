# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Create an LE Audio Broadcast Transmitter against a RootCanal virtual controller.

This example connects a Google Bumble host to a running RootCanal instance over a
TCP HCI transport and exercises the ISO_BROADCAST (LE Audio broadcaster) path:

  1. Verify the controller exposes the "Isochronous Broadcaster" LE feature and
     the HCI_LE_Create_BIG / HCI_LE_Terminate_BIG commands.
  2. Set up an advertising set and a periodic advertising train.
  3. Create a Broadcast Isochronous Group (BIG) on that train, which allocates
     one or more BIS connections.
  4. Configure the ISO data path for a BIS, and finally terminate the BIG.

RootCanal must be running first, for example:

    bazel run //:rootcanal    (listens on HCI port 6402 by default)

Run with uv (which provisions `bumble` and Python automatically):

    uv run --with bumble python examples/python/broadcast_transmitter.py
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from bumble import device, hci
from bumble.transport import open_transport

ROOTCANAL_HCI = "tcp-client:127.0.0.1:6402"  # RootCanal HCI TCP server.

# Number of BIS (Broadcast Isochronous Streams) to create in the BIG.
NUM_BIS = 2

# BIG parameters (Vol 4, Part E § 7.8.65 LE Create BIG).
SDU_INTERVAL_US = 10_000
MAX_SDU = 40
MAX_TRANSPORT_LATENCY_MS = 65
RTN = 4


async def create_device(transport: str) -> AsyncGenerator[device.Device, None]:
    """Open the transport and create a Bumble device over it."""
    async with await open_transport(transport) as (hci_source, hci_sink):
        device_config = device.DeviceConfiguration(
            name="Bumble Broadcast Transmitter",
        )
        controller = device.Device.from_config_with_hci(device_config, hci_source, hci_sink)
        await controller.power_on()
        yield controller


async def main() -> None:
    async for controller in create_device(ROOTCANAL_HCI):
        await controller.reset()

        # 1. Verify the Isochronous Broadcaster feature and commands are supported.
        le_features = controller.host.local_le_features
        isochronous_broadcaster = bool(
            le_features & (1 << hci.LeFeature.ISOCHRONOUS_BROADCASTER)
        )
        supported_commands = controller.host.supported_commands
        create_big_supported = hci.HCI_LE_CREATE_BIG_COMMAND in supported_commands
        terminate_big_supported = hci.HCI_LE_TERMINATE_BIG_COMMAND in supported_commands

        print("Isochronous Broadcaster LE feature:", isochronous_broadcaster)
        print("HCI_LE_Create_BIG supported:      ", create_big_supported)
        print("HCI_LE_Terminate_BIG supported:   ", terminate_big_supported)

        if not (isochronous_broadcaster and create_big_supported and terminate_big_supported):
            raise RuntimeError(
                "RootCanal does not expose the Isochronous Broadcaster feature. Build it "
                "with the Isochronous Broadcaster feature enabled (see changed.md)."
            )

        # 2. Create a (non-connectable) advertising set with a periodic advertising train.
        print("\nSetting up extended + periodic advertising...")
        advertising_set = await controller.create_advertising_set(
            advertising_parameters=device.AdvertisingParameters(
                advertising_event_properties=device.AdvertisingEventProperties(
                    is_connectable=False,
                    include_tx_power=True,
                ),
                primary_advertising_interval_min=100,  # 62.5 ms units -> ~6.25 ms
                primary_advertising_interval_max=100,
                advertising_sid=1,
            ),
            periodic_advertising_parameters=device.PeriodicAdvertisingParameters(
                periodic_advertising_interval_min=100,
                periodic_advertising_interval_max=100,
            ),
            auto_restart=True,
            auto_start=True,
        )
        await advertising_set.start_periodic()
        print(f"Advertising set #{advertising_set.advertising_handle} started, "
              f"periodic advertising running")

        # 3. Create the BIG (Broadcast Isochronous Group).
        print("\nCreating BIG...")
        big_parameters = device.BigParameters(
            num_bis=NUM_BIS,
            sdu_interval=SDU_INTERVAL_US,
            max_sdu=MAX_SDU,
            max_transport_latency=MAX_TRANSPORT_LATENCY_MS,
            rtn=RTN,
            phy=hci.PhyBit.LE_1M,
        )
        big = await controller.create_big(
            advertising_set,
            parameters=big_parameters,
        )
        print(f"BIG #{big.big_handle} established:")
        print(f"  BIS connection handles : {[link.handle for link in big.bis_links]}")
        print(f"  IAC/NSE/BN/PTO/IRC     : {big.nse}/{big.bn}/{big.pto}/{big.irc}")
        print(f"  Max_PDU                : {big.max_pdu}")
        print(f"  ISO_Interval           : {big.iso_interval} ms")

        # 4. Configure the ISO data path on the first BIS so that the host can
        #    start sending BIS SDUs toward the controller.
        bis = big.bis_links[0]
        await bis.setup_data_path(direction=bis.Direction.HOST_TO_CONTROLLER)
        print(f"ISO data path configured on BIS {bis.handle} (host -> controller)")

        # 5. Terminate the BIG.
        await big.terminate()
        print("\nBIG terminated.")

        await controller.power_off()


if __name__ == "__main__":
    asyncio.run(main())