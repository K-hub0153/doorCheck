import os
import socket
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QPushButton

from src.network import speaker_sn_connection as connection
from src.network.device_info import DEVICE_TYPE_OUTDOOR_SMART_HORN, DEVICE_TYPE_SMART_HORN
from src.network.mdns_discovery import DeviceDiscoveryListener
from src.network import speaker_sn_dhcp as dhcp
from src.network.speaker_sn_dhcp import DhcpLeasePool, parse_dhcp_options
from src.ui.startup_dialog import StartupDialog


APP = QApplication.instance() or QApplication([])


def dhcp_request(message_type=1, mac=b'\x02\x00\x00\x00\x00\x01', extra=b''):
    header = bytearray(236)
    header[:3] = b'\x01\x01\x06'
    header[4:8] = b'\x12\x34\x56\x78'
    header[28:34] = mac
    return bytes(header) + dhcp.DHCP_COOKIE + b'\x35\x01' + bytes([message_type]) + extra + b'\xff'


class SpeakerSNCompareTests(unittest.TestCase):
    @patch.object(connection, 'physical_ethernet_names', return_value={'Ethernet': 9})
    def test_selects_only_active_physical_ethernet(self, _names):
        selected = connection.select_ethernet_interface({
            'Ethernet': (True, ('192.168.137.1/24',)),
            'Wi-Fi': (True, ('192.168.137.2/24',)),
        })
        self.assertEqual('Ethernet', selected)

    @patch.object(connection, 'physical_ethernet_names', return_value={
        'Ethernet': 9, 'USB Ethernet': 10,
    })
    def test_refuses_ambiguous_physical_ethernet_interfaces(self, _names):
        selected = connection.select_ethernet_interface({
            'Ethernet': (True, ('192.168.137.1/24',)),
            'USB Ethernet': (True, ('10.0.0.1/24',)),
        })
        self.assertIsNone(selected)

    def test_serial_comparison_requires_three_nonempty_equal_values(self):
        self.assertTrue(connection.compare_serial_numbers(' SN001 ', 'SN001', 'SN001 '))
        self.assertFalse(connection.compare_serial_numbers('SN001', 'SN001', 'SN002'))
        self.assertFalse(connection.compare_serial_numbers('', '', ''))

    def test_original_discovery_parser_recognizes_indoor_and_outdoor_speakers(self):
        found = []
        listener = DeviceDiscoveryListener(
            on_device_found=found.append,
            device_types=[DEVICE_TYPE_SMART_HORN, DEVICE_TYPE_OUTDOOR_SMART_HORN],
        )

        class ServiceInfo:
            name = 'speaker._mqtt._tcp.local.'
            port = 1883
            properties = {
                b'device': b'{"sn":"SN001","type":"OUTDOOR_SMART_HORN"}',
            }

            @staticmethod
            def parsed_addresses():
                return ['192.168.137.20']

        listener._process_service(ServiceInfo())
        self.assertEqual(1, len(found))
        self.assertEqual('SN001', found[0].sn)
        self.assertEqual(DEVICE_TYPE_OUTDOOR_SMART_HORN, found[0].type)
        self.assertEqual('192.168.137.20', found[0].ip)

    def test_startup_dialog_has_two_column_compare_entry(self):
        dialog = StartupDialog()
        buttons = dialog.findChildren(QPushButton)
        compare = next(button for button in buttons if '序列号比对工具' in button.text())
        compare.click()
        self.assertEqual('speaker_sn_compare', dialog.selected_mode)
        dialog.close()

    def test_dhcp_discover_offer_and_request_ack(self):
        pool = DhcpLeasePool()
        mac = b'\x02\x00\x00\x00\x00\x01'

        def request(message_type, extra=b''):
            header = bytearray(236)
            header[:3] = b'\x01\x01\x06'
            header[4:8] = b'\x12\x34\x56\x78'
            header[28:34] = mac
            return bytes(header) + b'\x63\x82\x53\x63' + b'\x35\x01' + bytes([message_type]) + extra + b'\xff'

        offer = pool.handle(request(1))
        self.assertEqual(2, parse_dhcp_options(offer)[53][0])
        assigned = offer[16:20]
        wanted = b'\x32\x04' + assigned + b'\x36\x04' + socket.inet_aton('172.31.250.1')
        ack = pool.handle(request(3, wanted))
        self.assertEqual(5, parse_dhcp_options(ack)[53][0])
        self.assertEqual(assigned, ack[16:20])

    def test_dhcp_ignores_unrelated_server_and_refuses_unoffered_request(self):
        pool = DhcpLeasePool()
        header = bytearray(236)
        header[:3] = b'\x01\x01\x06'
        header[28:34] = b'\x02\x00\x00\x00\x00\x01'
        base = bytes(header) + b'\x63\x82\x53\x63'
        discover = base + b'\x35\x01\x01\xff'
        self.assertEqual(2, parse_dhcp_options(pool.handle(discover))[53][0])
        requested = socket.inet_aton('172.31.250.100')
        request = (base + b'\x35\x01\x03\x32\x04' + requested
                   + b'\x36\x04' + socket.inet_aton('172.31.250.99') + b'\xff')
        self.assertIsNone(pool.handle(request))

        unoffered = DhcpLeasePool().handle(base + b'\x35\x01\x03\x32\x04' + requested + b'\xff')
        self.assertEqual(6, parse_dhcp_options(unoffered)[53][0])

    def test_dhcp_lease_is_reused_and_other_mac_gets_different_ip(self):
        pool = DhcpLeasePool()
        first = pool.handle(dhcp_request())
        repeated = pool.handle(dhcp_request())
        second = pool.handle(dhcp_request(mac=b'\x02\x00\x00\x00\x00\x02'))
        self.assertEqual(first[16:20], repeated[16:20])
        self.assertNotEqual(first[16:20], second[16:20])
        self.assertEqual(b'\x12\x34\x56\x78', first[4:8])
        self.assertNotIn(3, parse_dhcp_options(first))
        self.assertNotIn(6, parse_dhcp_options(first))

    def test_dhcp_acknowledges_client_lease_after_server_restarts(self):
        mac = b'\x02\x00\x00\x00\x00\x01'
        old_pool = DhcpLeasePool()
        old_offer = old_pool.handle(dhcp_request(mac=mac))
        retained_address = old_offer[16:20]

        restarted_pool = DhcpLeasePool()
        request = dhcp_request(3, mac=mac, extra=b'\x32\x04' + retained_address)
        ack = restarted_pool.handle(request)

        self.assertEqual(5, parse_dhcp_options(ack)[53][0])
        self.assertEqual(retained_address, ack[16:20])
        self.assertIn(mac, restarted_pool.leases)

    def test_dhcp_rejects_malformed_packet_and_relay(self):
        pool = DhcpLeasePool()
        self.assertIsNone(pool.handle(b'bad'))
        packet = bytearray(dhcp_request())
        packet[24:28] = socket.inet_aton('10.1.1.1')
        self.assertIsNone(pool.handle(bytes(packet)))
        self.assertIsNone(pool.handle(dhcp_request()[:-1] + b'\x32\xff'))
        self.assertIsNone(pool.handle(dhcp_request(extra=b'\x35\x01\x01')))

    def test_dhcp_release_frees_lease(self):
        pool = DhcpLeasePool()
        offer = pool.handle(dhcp_request())
        self.assertEqual(1, len(pool.leases))
        pool.handle(dhcp_request(7, extra=b'\x32\x04' + offer[16:20]))
        self.assertEqual(0, len(pool.leases))

    def test_dhcp_decline_quarantines_address_and_expired_leases_are_reused(self):
        now = [0]
        pool = DhcpLeasePool(clock=lambda: now[0])
        offer = pool.handle(dhcp_request())
        pool.handle(dhcp_request(4, extra=b'\x32\x04' + offer[16:20]))
        next_offer = pool.handle(dhcp_request())
        self.assertNotEqual(offer[16:20], next_offer[16:20])
        now[0] = 601
        after_expiry = pool.handle(dhcp_request())
        self.assertEqual(offer[16:20], after_expiry[16:20])

    def test_dhcp_server_filters_receive_interface_and_pins_send_interface(self):
        server = dhcp.DirectDhcpServer(17)
        server.stop_event.set()
        fake_socket = MagicMock()
        with patch.object(dhcp.socket, 'socket', return_value=fake_socket), \
                patch.object(dhcp.socket, 'SO_EXCLUSIVEADDRUSE', 0xFFFFFFFB, create=True):
            server.run()
        self.assertIsNone(server.error)
        fake_socket.setsockopt.assert_any_call(socket.IPPROTO_IP, dhcp.WINDOWS_IP_IFLIST, 1)
        fake_socket.setsockopt.assert_any_call(socket.IPPROTO_IP, dhcp.WINDOWS_IP_ADD_IFLIST,
                                              __import__('struct').pack('I', 17))
        fake_socket.setsockopt.assert_any_call(socket.IPPROTO_IP, dhcp.WINDOWS_IP_UNICAST_IF,
                                              socket.htonl(17))

    @patch.object(connection.platform, 'system', return_value='Windows')
    @patch.object(connection.subprocess, 'run')
    def test_windows_adapter_discovery_reads_chinese_name_and_index(self, run, _system):
        connection.physical_ethernet_names.cache_clear()
        run.return_value.stdout = '{"Name":"生产以太网","InterfaceIndex":17}'
        self.assertEqual({'生产以太网': 17}, connection.physical_ethernet_names(123))
        connection.physical_ethernet_names.cache_clear()

    @patch.object(dhcp.platform, 'system', return_value='Windows')
    def test_network_setup_refuses_existing_ip_without_running_powershell(self, _system):
        with tempfile.TemporaryDirectory() as directory:
            network = dhcp.WindowsEthernetProvisioner('Ethernet', 17, directory)
            with patch.object(network, '_run_powershell') as powershell:
                with self.assertRaises(RuntimeError):
                    network.start(('10.0.0.2/24',))
                powershell.assert_not_called()

    @patch.object(dhcp.platform, 'system', return_value='Windows')
    def test_network_setup_refuses_conflicting_subnet(self, _system):
        with tempfile.TemporaryDirectory() as directory:
            network = dhcp.WindowsEthernetProvisioner('Ethernet', 17, directory)
            with patch.object(network, '_run_powershell') as powershell:
                with self.assertRaises(RuntimeError):
                    network.start((), ('172.31.250.3/16',))
                powershell.assert_not_called()

    @patch.object(dhcp.platform, 'system', return_value='Windows')
    def test_network_start_and_restore_cleanup_backup(self, _system):
        with tempfile.TemporaryDirectory() as directory:
            network = dhcp.WindowsEthernetProvisioner('Ethernet', 17, directory)
            state = {'Dhcp': 'Enabled', 'Name': 'Ethernet', 'Guid': 'guid17', 'HasGateway': False}
            with patch.object(network, '_query_state', return_value=state), \
                    patch.object(network, '_run_powershell') as powershell, \
                    patch.object(dhcp, 'DirectDhcpServer') as server_type, \
                    patch.object(dhcp.time, 'sleep'):
                server_type.return_value.ready.wait.return_value = True
                server_type.return_value.error = None
                network.start(('169.254.2.2/16',))
                self.assertTrue(os.path.exists(network.backup_path))
                self.assertTrue(network.changed)
                network.restore()
                self.assertFalse(os.path.exists(network.backup_path))
                self.assertFalse(network.changed)
                server_type.return_value.stop.assert_called_once()
                self.assertEqual(2, powershell.call_count)

    @patch.object(connection, 'physical_ethernet_names', return_value={'Ethernet': 17})
    @patch.object(connection, 'network_interfaces', return_value={
        'Wi-Fi': (True, ('192.168.1.20/24',)),
    })
    @patch.object(connection, 'ServiceBrowser')
    @patch.object(connection, 'Zeroconf')
    def test_worker_never_starts_mdns_on_wifi_without_rj45(self, zeroconf, browser,
                                                          _interfaces, _ethernet):
        worker = connection.SpeakerConnectionWorker('_mqtt._tcp.local.')
        worker.stop_event.wait = lambda _seconds: worker.stop_event.set()
        worker.run()
        zeroconf.assert_not_called()
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
