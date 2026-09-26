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

from test.LL.BIS.BRD.BV_01_C import Test as BVBroadcastSetupAndData


class Test(BVBroadcastSetupAndData):

    # LL/BIS/BRD/BV-24-C [BIS Broadcast Setup and Data, Broadcaster role,
    # BN = 1, Encryption Enabled]
    #
    # This test uses default BIG parameters with BN = 1 and Encryption enabled,
    # as specified in LL.TS.p28 Table 4.11-3.
    Num_BIS = 0x01
    BN = 1
    Max_SDU = 32  # Default_Data_Size, Unframed, BN = 1
    Encryption = hci.Enable.ENABLED
    Broadcast_Code = bytearray([0x22] * 16)

    # LL/BIS/BRD/BV-24-C [BIS Broadcast Setup and Data, Broadcaster role]
    async def test(self):
        # Test parameters.
        big_handle = 0x09
        controller = self.controller

        # Prelude: configure the IUT as a broadcaster with a periodic
        # advertising train (the pre-requisite for creating a BIG).
        await self.setup_extended_advertiser()

        # 1. Create a single BIS with the default parameters, BN = 1, and
        # Encryption enabled using HCI_LE_Create_BIG_Test.
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
        # successful HCI_LE_Create_BIG_Complete event. The BIG_Handle is as
        # provided by the Upper Tester, BIS_Count equals Num_BIS, and the
        # Controller BIS_Connection_Handle values are valid.
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
        # advertising train of the IUT. Because Encryption is enabled, the
        # BIGInfo includes the encryption fields and carries the Encryption bit
        # set (cf. LL.TS.p28 Table 4.11-3).
        big_info = await self.expect_big_info_advertising()
        self.assertEqual(big_info.source_address, controller.address)
        self.assertEqual(big_info.sid, self.Advertising_SID)
        self.assertEqual(big_info.num_bis, self.Num_BIS)
        self.assertEqual(big_info.max_sdu, self.Max_SDU)
        self.assertEqual(big_info.encryption, int(self.Encryption))

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

        # 5. The Lower Tester receives the BIS Data PDU broadcast by the IUT
        # on the virtual air, carrying the BIS SDU as sent by the Host, and the
        # controller reports it as a completed packet to the Host.
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