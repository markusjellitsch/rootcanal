# Copyright 2026 The Android Open Source Project
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

from rootcanal.packets import hci
from rootcanal.packets import ll
import random
import unittest
from rootcanal.packets.hci import ErrorCode
from rootcanal.bluetooth import Address
from test.controller_test import ControllerTest


class Test(ControllerTest):

    # Test parameters used by the BIS Broadcaster role (LL/BIS/BRD).
    Advertising_Handle = 0x0F
    Advertising_SID = 0x02
    Num_BIS = 0x01
    Num_BIS_BN1 = 0x01
    SDU_Interval = 10000  # 10ms
    ISO_Interval = 8  # 10ms in 1.25ms units
    NSE = 4
    Max_SDU = 32
    Max_PDU = 32
    PHY = hci.SecondaryPhyType.LE_1M
    Packing = hci.Packing.SEQUENTIAL
    Framing = hci.Enable.DISABLED
    BN = 2
    IRC = 2
    PTO = 0
    Encryption = hci.Enable.DISABLED
    Broadcast_Code = bytearray([0x00] * 16)

    # The controller assigns BIS connection handles in the range
    # [0x0D00, 0x0DFE] (3328..3582).
    Min_BIS_Connection_Handle = 0x0D00
    Max_BIS_Connection_Handle = 0x0DFE

    async def setup_extended_advertiser(self):
        """Set up a periodic advertising train, the pre-requisite for a
        broadcast isochronous group (BIG)."""

        controller = self.controller

        # 1. Configure the IUT as a broadcaster with periodic advertising:
        # set extended advertising parameters on the advertising set.
        controller.send_cmd(
            hci.LeSetExtendedAdvertisingParametersV1(
                advertising_handle=self.Advertising_Handle,
                advertising_event_properties=hci.AdvertisingEventProperties(
                    include_adva=True
                ),
                primary_advertising_interval_min=512,
                primary_advertising_interval_max=512,
                primary_advertising_channel_map=0x7,
                own_address_type=hci.OwnAddressType.PUBLIC_DEVICE_ADDRESS,
                peer_address_type=hci.PeerAddressType.PUBLIC_DEVICE_OR_IDENTITY_ADDRESS,
                peer_address=Address(),
                advertising_filter_policy=hci.AdvertisingFilterPolicy.ALL_DEVICES,
                advertising_tx_power=0,
                primary_advertising_phy=hci.PrimaryPhyType.LE_1M,
                secondary_advertising_max_skip=0,
                secondary_advertising_phy=hci.SecondaryPhyType.LE_1M,
                advertising_sid=self.Advertising_SID,
                scan_request_notification_enable=False,
            )
        )

        await self.expect_evt(
            hci.LeSetExtendedAdvertisingParametersV1Complete(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

        # 2. Configure the periodic advertising train parameters.
        controller.send_cmd(
            hci.LeSetPeriodicAdvertisingParametersV1(
                advertising_handle=self.Advertising_Handle,
                periodic_advertising_interval_min=256,
                periodic_advertising_interval_max=256,
                include_tx_power=False,
            )
        )

        await self.expect_evt(
            hci.LeSetPeriodicAdvertisingParametersV1Complete(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

        # 3. Enable extended advertising for the advertising set.
        controller.send_cmd(
            hci.LeSetExtendedAdvertisingEnable(
                enable=hci.Enable.ENABLED,
                enabled_sets=[
                    hci.EnabledSet(
                        advertising_handle=self.Advertising_Handle,
                        duration=0,
                        max_extended_advertising_events=0,
                    )
                ],
            )
        )

        await self.expect_evt(
            hci.LeSetExtendedAdvertisingEnableComplete(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

        # 4. Enable the periodic advertising train.
        controller.send_cmd(
            hci.LeSetPeriodicAdvertisingEnable(
                enable=hci.Enable.ENABLED,
                include_adi=False,
                advertising_handle=self.Advertising_Handle,
            )
        )

        await self.expect_evt(
            hci.LeSetPeriodicAdvertisingEnableComplete(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

    # LL/BIS/BRD/BV-01-C [BIS Broadcast Setup and Data, Broadcaster role]
    async def test(self):
        # Test parameters.
        big_handle = 0x05
        controller = self.controller

        # Prelude: configure the IUT as a broadcaster with a periodic
        # advertising train (the pre-requisite for creating a BIG).
        await self.setup_extended_advertiser()

        # 1. The Upper Tester sends HCI_LE_Create_BIG_Test to establish a
        # single BIS using the default BIG parameters in LL.TS.p28 Table 4.11-3.
        # The IUT responds with a successful HCI_Command_Status event.
        controller.send_cmd(
            hci.LeCreateBigTest(
                big_handle=big_handle,
                advertising_handle=self.Advertising_Handle,
                num_bis=self.Num_BIS,
                sdu_interval=self.SDU_Interval,
                iso_interval=self.ISO_Interval,
                nse=self.NSE,
                max_sdu=self.Max_SDU,
                max_pdu=self.Max_PDU,
                phy=self.PHY,
                packing=self.Packing,
                framing=self.Framing,
                bn=self.BN,
                irc=self.IRC,
                pto=self.PTO,
                encryption=self.Encryption,
                broadcast_code=self.Broadcast_Code,
            )
        )

        await self.expect_evt(
            hci.LeCreateBigTestStatus(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

        # 2. The IUT finishes creating the BIG and the Upper Tester receives a
        # successful HCI_LE_Create_BIG_Complete event with the NSE, BN, PTO,
        # IRC, and Max_PDU values supplied in Step 1, and with the PHY used to
        # create the BIG.
        complete = await self.expect_evt(hci.LeCreateBigComplete)
        self.assertEqual(complete.status, ErrorCode.SUCCESS)
        self.assertEqual(complete.big_handle, big_handle)
        self.assertEqual(len(complete.connection_handle), self.Num_BIS)
        self.assertEqual(complete.nse, self.NSE)
        self.assertEqual(complete.bn, self.BN)
        self.assertEqual(complete.pto, self.PTO)
        self.assertEqual(complete.irc, self.IRC)
        self.assertEqual(complete.max_pdu, self.Max_PDU)
        self.assertEqual(complete.phy, self.PHY)
        for handle in complete.connection_handle:
            self.assertGreaterEqual(handle, self.Min_BIS_Connection_Handle)
            self.assertLessEqual(handle, self.Max_BIS_Connection_Handle)

        # 3. The Lower Tester receives the BIGInfo announced on the periodic
        # advertising train of the IUT, confirming the BIG configuration
        # (BIS_Count, Max_SDU, SID, ...).
        big_info = await self.expect_ll(
            ll.LeBigInfoAdvertisingPdu,
            ignored_pdus=[
                ll.LeExtendedAdvertisingPdu,
                ll.LePeriodicAdvertisingPdu,
            ],
        )
        self.assertEqual(big_info.source_address, controller.address)
        self.assertEqual(big_info.sid, self.Advertising_SID)
        self.assertEqual(big_info.num_bis, self.Num_BIS)
        self.assertEqual(big_info.max_sdu, self.Max_SDU)

        # 4. The Upper Tester sends ISO SDUs on the first BIS of the BIG.
        bis_connection_handle = complete.connection_handle[0]
        sdu = [random.randint(1, 251) for n in range(self.Max_SDU)]
        controller.send_iso(
            hci.IsoWithoutTimestamp(
                connection_handle=bis_connection_handle,
                pb_flag=hci.IsoPacketBoundaryFlag.COMPLETE_SDU,
                packet_sequence_number=0,
                payload=sdu,
            )
        )

        # 5. The Lower Tester receives the BIS Data PDU broadcast by the IUT on
        # the virtual air, carrying the BIS SDU, and the controller reports it
        # as a completed packet to the Host.
        await self.expect_ll(
            ll.LeBroadcastIsochronousPdu(
                source_address=controller.address,
                destination_address=Address(),
                big_handle=big_handle,
                bis_id=1,
                sequence_number=0,
                data=sdu,
            )
        )

        await self.expect_evt(
            hci.NumberOfCompletedPackets(
                completed_packets=[
                    hci.CompletedPackets(
                        connection_handle=bis_connection_handle,
                        host_num_of_completed_packets=1,
                    )
                ]
            )
        )

        # 6. The Upper Tester sends an HCI_LE_Terminate_BIG command to the IUT.
        # The IUT responds with a successful HCI_Command_Status event and then
        # an HCI_LE_Terminate_BIG_Complete event with the given reason.
        controller.send_cmd(
            hci.LeTerminateBig(
                big_handle=big_handle, reason=ErrorCode.SUCCESS
            )
        )

        await self.expect_evt(
            hci.LeTerminateBigStatus(
                status=ErrorCode.SUCCESS, num_hci_command_packets=1
            )
        )

        await self.expect_evt(
            hci.LeTerminateBigComplete(
                big_handle=big_handle, reason=ErrorCode.SUCCESS
            )
        )