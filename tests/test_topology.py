"""UVHS multi-host discovery must not confuse an unattached board with free rows."""
import unittest

from fpga_scheduler.remote.runtime import parse_fpgas
from fpga_scheduler.remote.topology import merge_inventory, parse_physical_fpgas, parse_topology


BOARD_WIDTHS = (17, 22, 17, 35, 27, 18, 17, 17)
BOARD_HEADER = ('System Name', 'Topology Board ID', 'Device Type', 'HW ID CODE',
                'HW Location', 'Mapped Board ID', 'Used by Host', 'Used by User', 'Duration')
FPGA_WIDTHS = (17, 22, 22, 18, 17, 17)
FPGA_HEADER = ('System Name', 'Topology Board ID', 'Topology Fpga Id',
               'Mapped Board ID', 'Device Type', 'Used by Host', 'Used by User')
STATUS_HEADER = 'System Name      Board   FPGA    Power   Status                Owner                 Booked'


def fixed_row(values, widths):
    return ''.join(str(value).ljust(width) for value, width in zip(values, widths)) + str(values[-1])


def topology_text(boards=None, fpgas=None):
    if boards is None:
        boards = [('uvhs-0', 'TB2', 'V1', 'hardware-b', '', '', '', '', '32m'),
                  ('uvhs-0', 'TB1', 'V1', 'hardware-a', '', 'B0', '', '', '1m')]
    if fpgas is None:
        fpgas = [('uvhs-0', tb, f'F{fpga}', '', 'V1', '', '')
                 for tb in ('TB2', 'TB1') for fpga in range(4)]
    return ('Command: topology -query -tb -hw\nTopology Data\n'
            + fixed_row(BOARD_HEADER, BOARD_WIDTHS) + '\n' + '-' * 200 + '\n'
            + '\n'.join(fixed_row(row, BOARD_WIDTHS) for row in boards)
            + '\n\nCommand: topology -query -fpgas -tb\nTopology Data\n'
            + fixed_row(FPGA_HEADER, FPGA_WIDTHS) + '\n' + '-' * 130 + '\n'
            + '\n'.join(fixed_row(row, FPGA_WIDTHS) for row in fpgas) + '\n')


def status_text(rows=None, physical=True):
    if rows is None:
        rows = [f'uvhs-0 {board} F{fpga} on link down false'
                for board in ('TB2', 'TB1') for fpga in range(4)]
    return ('Command: query -fpgas -all' + (' -tb' if physical else '')
            + '\nInstalled DUT FPGA Status:\n' + STATUS_HEADER + '\n'
            + '-' * 97 + '\n' + '\n'.join(rows) + '\n')


class TopologyTests(unittest.TestCase):
    def merge(self, runtime=None, physical=None, topology=None, mapping=None, local_host='localhost.localdomain'):
        return merge_inventory({} if runtime is None else runtime,
                               parse_physical_fpgas(status_text()) if physical is None else physical,
                               parse_topology(topology_text()) if topology is None else topology,
                               {0: 'TB1', 1: 'TB2'} if mapping is None else mapping,
                               local_host=local_host)

    def test_combined_transcript_uses_headers_to_keep_three_tables_separate(self):
        transcript = topology_text() + status_text() + status_text(
            ['uvhs-0 B0 F1 on link up other false'], physical=False)
        topology = parse_topology(transcript)
        physical = parse_physical_fpgas(transcript)
        merged = self.merge(parse_fpgas(transcript), physical, topology)
        self.assertEqual(len(physical), 8)
        self.assertEqual(len(merged), 8)
        self.assertEqual(merged[1]['owner'], 'other')
        self.assertEqual(merged[4]['topology_board'], 'TB2')
        self.assertFalse(merged[4]['attached'])
        self.assertEqual(merged[4]['sources'], ['physical', 'topology'])
        self.assertEqual(merged[4]['board_owners'], [])
        self.assertTrue(merged[1]['attached'])

    def test_wrapped_host_user_and_pid_reassemble_without_token_truncation(self):
        rows = [('uvhs-0', 'TB1', 'F0', '', 'V1', 'localhost.local', 'chengweiwei(555'),
                ('', '', '', '', '', 'domain', '60)')]
        board_rows = [('uvhs-0', 'TB1', 'V1', 'id', '', 'B0', 'localhost.local', 'user(123', '1m'),
                      ('', '', '', '', '', '', 'domain', '45)', '')]
        parsed = parse_topology(topology_text(board_rows, rows))
        self.assertEqual(parsed['boards']['TB1']['used_host'], 'localhost.localdomain')
        self.assertEqual(parsed['boards']['TB1']['used_user'], 'user(12345)')
        self.assertEqual(parsed['fpgas'][('TB1', 0)]['used_user'], 'chengweiwei(55560)')
        self.assertIn('chengweiwei(55560)', self.merge(topology=parsed)[0]['owner'])

    def test_vendor_header_and_data_width_mismatch_keeps_complete_busy_owner(self):
        # Actual P3 data uses 17 for the mapped-board column; its header uses
        # 18. This shifts every following value one character to the left.
        transcript = topology_text()
        board = fixed_row(('uvhs-0', 'TB1', 'V1', 'id', '', 'B0',
                           'localhost.local', 'chengweiwei(555', '36s'),
                          (17, 22, 17, 35, 27, 17, 17, 17))
        board_continuation = fixed_row(('', '', '', '', '', '', 'domain', '60)', ''),
                                       (17, 22, 17, 35, 27, 17, 17, 17))
        fpga = fixed_row(('uvhs-0', 'TB1', 'F0', '', 'V1', 'localhost.local', 'chengweiwei(555'),
                         (17, 22, 22, 17, 17, 17))
        fpga_continuation = fixed_row(('', '', '', '', '', 'domain', '60)'),
                                      (17, 22, 22, 17, 17, 17))
        for original in transcript.splitlines():
            if original.startswith('uvhs-0') and original.split()[1:3] == ['TB1', 'V1']:
                transcript = transcript.replace(original, board + '\n' + board_continuation)
            if original.startswith('uvhs-0') and original.split()[1:3] == ['TB1', 'F0']:
                transcript = transcript.replace(original, fpga + '\n' + fpga_continuation)
        parsed = parse_topology(transcript)
        self.assertEqual(parsed['boards']['TB1']['mapped_board'], 'B0')
        self.assertEqual(parsed['boards']['TB1']['used_host'], 'localhost.localdomain')
        self.assertEqual(parsed['fpgas'][('TB1', 0)]['used_user'], 'chengweiwei(55560)')
        self.assertEqual(self.merge(topology=parsed)[0]['owner'], 'chengweiwei(55560)')

    def test_foreign_host_blocks_its_fpga_even_without_user(self):
        topology = parse_topology(topology_text())
        topology['fpgas'][('TB1', 0)]['used_host'] = 'another-host'
        topology['fpgas'][('TB1', 1)]['used_host'] = 'localhost.localdomain'
        merged = self.merge(topology=topology)
        self.assertEqual(merged[0]['owner'], 'host:another-host')
        self.assertEqual(merged[1]['owner'], '')

    def test_board_foreign_host_blocks_siblings_but_local_aggregate_users_do_not(self):
        topology = parse_topology(topology_text())
        topology['boards']['TB1'].update(used_host='another-host', used_user='aggregate-owner')
        merged = self.merge(topology=topology)
        self.assertTrue(all(merged[p]['owner'] == 'host:another-host' for p in range(4)))
        topology['boards']['TB1']['used_host'] = 'localhost.localdomain'
        merged = self.merge(topology=topology)
        self.assertTrue(all(not merged[p]['owner'] for p in range(4)))
        self.assertTrue(all(merged[p]['board_owners'] == ['aggregate-owner'] for p in range(4)))

    def test_wrapped_aggregate_users_survive_as_shared_clock_metadata(self):
        rows = [('uvhs-0', 'TB1', 'V1', 'id', '', 'B0', '', 'someone(123),fi', '1m'),
                ('', '', '', '', '', '', '', 'rst(456)', '')]
        topology = parse_topology(topology_text(boards=rows))
        merged = self.merge(topology=topology)
        self.assertEqual(merged[0]['board_owners'], ['someone(123),first(456)'])
        self.assertTrue(all(not port['owner'] for port in merged.values()))

    def test_busy_from_every_source_is_retained_even_when_other_sources_say_free(self):
        physical = parse_physical_fpgas(status_text())
        physical[('TB1', 0)]['owner'] = 'physical-owner'
        physical[('TB1', 1)]['booked'] = True
        topology = parse_topology(topology_text())
        topology['fpgas'][('TB1', 2)]['used_user'] = 'topology-owner(123)'
        runtime = {3: dict(owner='runtime-owner', power='on', booked=True)}
        merged = self.merge(runtime, physical, topology)
        self.assertEqual(merged[0]['owner'], 'physical-owner')
        self.assertTrue(merged[1]['booked'])
        self.assertEqual(merged[2]['owner'], 'topology-owner(123)')
        self.assertEqual(merged[3]['owner'], 'runtime-owner')
        self.assertTrue(merged[3]['booked'])

    def test_conflicting_busy_owners_preserve_both(self):
        physical = parse_physical_fpgas(status_text())
        physical[('TB1', 0)]['owner'] = 'first'
        runtime = {0: dict(owner='second', power='on', booked=False)}
        self.assertEqual(self.merge(runtime, physical)[0]['owners'], ['first', 'second'])

    def test_power_off_in_either_status_source_blocks_slot(self):
        physical = parse_physical_fpgas(status_text())
        physical[('TB1', 1)]['power'] = 'off'
        runtime = {0: dict(owner='', power='off', booked=False),
                   1: dict(owner='', power='on', booked=False)}
        merged = self.merge(runtime, physical)
        self.assertEqual(merged[0]['power'], 'off')
        self.assertEqual(merged[1]['power'], 'off')

    def test_runtime_row_alone_cannot_make_missing_physical_slot_free(self):
        physical = parse_physical_fpgas(status_text())
        del physical[('TB1', 0)]
        runtime = {0: dict(owner='', power='on', booked=False)}
        self.assertNotIn(0, self.merge(runtime, physical))

    def test_discovery_row_missing_never_makes_physical_slot_free(self):
        topology = parse_topology(topology_text())
        del topology['fpgas'][('TB1', 0)]
        self.assertNotIn(0, self.merge(topology=topology))
        del topology['boards']['TB1']
        self.assertEqual(set(self.merge(topology=topology)), {4, 5, 6, 7})

    def test_unconfigured_boards_never_get_numbers_from_order_or_tb_offset(self):
        merged = self.merge(mapping={3: 'TB2'})
        self.assertEqual(set(merged), {12, 13, 14, 15})
        self.assertEqual(merged[12]['board'], 'B3')

    def test_board_mapping_conflict_is_rejected_in_both_directions(self):
        for mapping in ({0: 'TB2'}, {1: 'TB1'}):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(RuntimeError, 'mapping conflict'):
                self.merge(mapping=mapping)
        topology = parse_topology(topology_text())
        topology['boards']['TB2']['mapped_board'] = 'B0'
        with self.assertRaisesRegex(RuntimeError, 'mapping conflict|multiple physical boards'):
            self.merge(topology=topology)

    def test_fpga_mapping_cannot_redirect_the_configured_target(self):
        topology = parse_topology(topology_text())
        topology['fpgas'][('TB2', 0)]['mapped_board'] = 'B0.F0'
        with self.assertRaisesRegex(RuntimeError, 'FPGA mapping conflict'):
            self.merge(topology=topology)

    def test_duplicate_and_invalid_configuration_fail_closed(self):
        for mapping in ({0: 'TB1', 1: 'TB1'}, {True: 'TB1'}, {}, {0: 'B1'}):
            with self.subTest(mapping=mapping), self.assertRaisesRegex(RuntimeError, 'one-to-one'):
                self.merge(mapping=mapping)

    def test_duplicate_or_missing_topology_tables_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'duplicate topology'):
            parse_topology(topology_text() + topology_text())
        with self.assertRaisesRegex(RuntimeError, 'both board and FPGA'):
            parse_topology(status_text())

    def test_duplicate_rows_and_malformed_physical_rows_are_rejected(self):
        row = 'uvhs-0 TB1 F0 on link down false'
        with self.assertRaisesRegex(RuntimeError, 'duplicate physical'):
            parse_physical_fpgas(status_text([row, row]))
        for row in ('uvhs-0 TB1 F0 on link down',
                    'uvhs-0 TB1 F0 unknown link down false',
                    'uvhs-0 TB1 F9 on link down false'):
            with self.subTest(row=row), self.assertRaisesRegex(RuntimeError, 'invalid physical'):
                parse_physical_fpgas(status_text([row]))

    def test_nonstatus_tb_rows_are_not_parsed_as_power_owner_rows(self):
        with self.assertRaisesRegex(RuntimeError, 'physical FPGA status'):
            parse_physical_fpgas(topology_text())


if __name__ == '__main__':
    unittest.main()
