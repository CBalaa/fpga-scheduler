"""Contract C3: bounded physical-DDR read planning (pure math, no hardware)."""
import io
import unittest

from fpga_scheduler.remote.ddr_window import (
    ADDRESS_SPACE, ELEMENT, WindowError, address, describe_reads, plan)

BASE = 0x80000000
# Physical logits address of the 87.3 MiB whole-model bin: 622 MiB above the base.
FAR = 0xa6e00080


def declaration(where, nbytes, name='C'):
    """A realistic output declaration; plan() only consumes address/nbytes."""
    return dict(version=1, name=name, address=hex(where) if isinstance(where, int) else where,
                nbytes=nbytes, dtype='|u1', shape=[nbytes])


def extract(outputs, read):
    """Mirror uvhs_session.capture_outputs: snapshot slices located by window_start."""
    snapshot = bytearray(read['window_size'])
    for index, item in enumerate(outputs):
        offset = int(item['address'], 16) - read['window_start']
        assert 0 <= offset and offset + item['nbytes'] <= len(snapshot), (offset, item)
        snapshot[offset:offset + item['nbytes']] = bytes([index + 1]) * item['nbytes']
    source = io.BytesIO(bytes(snapshot))
    extracted = []
    for item in outputs:
        source.seek(int(item['address'], 16) - read['window_start'])
        extracted.append(source.read(item['nbytes']))
    return extracted


class AddressTests(unittest.TestCase):
    def test_accepts_32_bit_hex_strings(self):
        for text, value in (('0x0', 0), ('0x80000000', BASE), ('0xa6e00080', FAR),
                            ('0xA6E00080', FAR), ('0xffffffff', 2 ** 32 - 1)):
            with self.subTest(text=text):
                self.assertEqual(address(text), value)

    def test_rejects_malformed_and_out_of_space_addresses(self):
        for value in ('', '0x', '0x-1', '0x 8000', '80000000', '0x100000000',
                      '0xffffffff0', '0x12zz', ' 0x8000', '0x8000\n', None, 0x80000000, True):
            with self.subTest(value=value), self.assertRaises(WindowError):
                address(value)
        self.assertTrue(issubclass(WindowError, ValueError))


class PrefixPlanTests(unittest.TestCase):
    def test_reads_from_base_through_highest_output_end_rounded_up(self):
        outputs = [declaration(0x80000105, 7), declaration(0x80000084, 3)]
        self.assertEqual(plan(outputs, BASE, 0x1000), dict(
            mode='prefix', window_start=BASE, window_size=0x140,
            element_low=0, element_high=4, memory='nanhu_xilinx.i_ddr[4:0]'))

    def test_single_output_on_first_element_reads_one_element(self):
        self.assertEqual(plan([declaration(BASE, 1)], BASE, 0x1000)['memory'],
                         'nanhu_xilinx.i_ddr[0:0]')
        self.assertEqual(plan([declaration(BASE, ELEMENT)], BASE, 0x1000)['window_size'],
                         ELEMENT)

    def test_aligned_end_does_not_add_an_element(self):
        read = plan([declaration(BASE + ELEMENT, ELEMENT)], BASE, 0x1000)
        self.assertEqual((read['window_start'], read['window_size']), (BASE, 2 * ELEMENT))
        self.assertEqual(read['memory'], 'nanhu_xilinx.i_ddr[1:0]')

    def test_lowest_output_does_not_shrink_a_prefix_read(self):
        read = plan([declaration(BASE + 0x400, 8)], BASE, 0x1000)
        self.assertEqual((read['window_start'], read['window_size']), (BASE, 0x440))
        self.assertEqual(read['element_low'], 0)
        self.assertEqual(read['memory'], 'nanhu_xilinx.i_ddr[16:0]')


class WindowPlanTests(unittest.TestCase):
    def test_reads_only_rounded_min_to_rounded_max(self):
        outputs = [declaration(0x80000105, 7), declaration(0x80000084, 3)]
        self.assertEqual(plan(outputs, BASE, 0x1000, 'window'), dict(
            mode='window', window_start=0x80000080, window_size=0xc0,
            element_low=2, element_high=4, memory='nanhu_xilinx.i_ddr[4:2]'))

    def test_window_geometry_is_consistent_with_element_indices(self):
        outputs = [declaration(0x80000105, 7), declaration(0x80000084, 3)]
        read = plan(outputs, BASE, 0x1000, 'window')
        self.assertEqual(read['window_start'] % ELEMENT, 0)
        self.assertEqual(read['window_size'] % ELEMENT, 0)
        self.assertEqual(read['window_start'],
                         BASE + read['element_low'] * ELEMENT)
        self.assertEqual(read['window_size'],
                         (read['element_high'] - read['element_low'] + 1) * ELEMENT)
        lowest = int(outputs[1]['address'], 16)
        self.assertEqual(read['window_start'], lowest - lowest % ELEMENT)
        self.assertEqual(read['window_start'] + read['window_size'],
                         0x80000140)

    def test_nonzero_low_element_math(self):
        # Same declaration the offline artifact tests use (0x80200404 = base + 128 KiB + 4).
        read = plan([declaration(0x80200404, 24)], BASE, 0x400000, 'window')
        self.assertEqual(read['window_start'], 0x80200400)
        self.assertEqual(read['window_size'], ELEMENT)
        self.assertEqual(read['element_low'], 0x8010)
        self.assertEqual(read['element_high'], 0x8010)
        self.assertEqual(read['memory'], 'nanhu_xilinx.i_ddr[32784:32784]')

    def test_second_output_inside_same_window_keeps_one_element(self):
        outputs = [declaration(0x80200404, 24), declaration(0x80200410, 8)]
        read = plan(outputs, BASE, 0x400000, 'window')
        self.assertEqual((read['window_start'], read['window_size']), (0x80200400, ELEMENT))
        self.assertEqual([item['address'] for item in outputs],
                         ['0x80200404', '0x80200410'])

    def test_window_starts_at_base_when_lowest_element_is_element_zero(self):
        read = plan([declaration(BASE + 4, 8)], BASE, 0x1000, 'window')
        self.assertEqual((read['window_start'], read['element_low']), (BASE, 0))


class ModeEquivalenceTests(unittest.TestCase):
    def test_prefix_and_window_are_identical_when_read_starts_at_base(self):
        for address_value in (BASE, BASE + 4, BASE + 60):
            with self.subTest(address=hex(address_value)):
                outputs = [declaration(address_value, 8)]
                prefix = plan(outputs, BASE, 0x1000, 'prefix')
                window = plan(outputs, BASE, 0x1000, 'window')
                shared = {key: value for key, value in prefix.items() if key != 'mode'}
                self.assertEqual({key: value for key, value in window.items() if key != 'mode'},
                                 shared)

    def test_prefix_never_reads_less_than_window(self):
        outputs = [declaration(0x80000200, 8), declaration(0x80000040, 8)]
        prefix = plan(outputs, BASE, 0x1000, 'prefix')
        window = plan(outputs, BASE, 0x1000, 'window')
        self.assertEqual(prefix['window_start'], BASE)
        self.assertEqual(window['window_start'], 0x80000040)
        self.assertLess(window['window_size'], prefix['window_size'])
        self.assertGreaterEqual(prefix['window_start'] + prefix['window_size'],
                                window['window_start'] + window['window_size'])


class BaseAndBoundaryTests(unittest.TestCase):
    def test_base_other_than_default_80000000(self):
        base = 0x40000000
        read = plan([declaration(0x40000084, 16)], base, 0x1000, 'window')
        self.assertEqual(read['window_start'], 0x40000080)
        self.assertEqual((read['window_size'], read['element_low'], read['element_high']),
                         (ELEMENT, 2, 2))
        self.assertEqual(read['memory'], 'nanhu_xilinx.i_ddr[2:2]')
        prefix = plan([declaration(0x40000084, 16)], base, 0x1000, 'prefix')
        self.assertEqual(prefix['window_start'], base)
        self.assertEqual(prefix['window_size'], 0xc0)
        self.assertEqual(prefix['memory'], 'nanhu_xilinx.i_ddr[2:0]')

    def test_output_ending_exactly_at_window_end_is_allowed(self):
        base, span = 0xffffffc0, 64
        read = plan([declaration('0xffffffff', 1)], base, span, 'window')
        self.assertEqual(read['window_start'], base)
        self.assertEqual(read['window_size'], ELEMENT)
        self.assertEqual(base + span, ADDRESS_SPACE)

    def test_far_logits_address_is_cheap_in_window_mode(self):
        span = FAR + 24 - BASE
        span = (span + ELEMENT - 1) // ELEMENT * ELEMENT
        outputs = [declaration(FAR, 24)]
        self.assertGreater(FAR - BASE, 600 * 1024 * 1024)
        window = plan(outputs, BASE, span, 'window')
        self.assertEqual(window['window_start'], FAR)
        self.assertEqual(window['window_size'], ELEMENT)
        self.assertEqual(window['element_low'], (FAR - BASE) // ELEMENT)
        self.assertEqual(window['memory'],
                         'nanhu_xilinx.i_ddr[' + str((FAR - BASE) // ELEMENT) + ':'
                         + str((FAR - BASE) // ELEMENT) + ']')
        prefix = plan(outputs, BASE, span, 'prefix')
        self.assertEqual(prefix['window_start'], BASE)
        self.assertEqual(prefix['window_size'], span)
        self.assertEqual(prefix['memory'],
                         'nanhu_xilinx.i_ddr[' + str(span // ELEMENT - 1) + ':0]')
        self.assertGreater(prefix['window_size'], 600 * 1024 * 1024)

    def test_unaligned_far_address_rounds_down_and_up(self):
        outputs = [declaration('0xa6e00084', 24)]
        span = 0xa6e00084 + 24 - BASE
        span = (span + ELEMENT - 1) // ELEMENT * ELEMENT
        read = plan(outputs, BASE, span, 'window')
        self.assertEqual(read['window_start'], 0xa6e00080)
        self.assertEqual(read['window_start'] + read['window_size'], 0xa6e000c0)
        self.assertEqual(read['window_size'], ELEMENT)


class ExtractionTests(unittest.TestCase):
    def test_extraction_offsets_map_window_snapshot_to_declared_ranges(self):
        outputs = [declaration(0x80000084, 8), declaration(0x80000102, 8, 'D')]
        read = plan(outputs, BASE, 0x1000, 'window')
        self.assertEqual(extract(outputs, read),
                         [bytes([1]) * 8, bytes([2]) * 8])

    def test_extraction_offsets_map_nonzero_low_snapshot(self):
        outputs = [declaration(0x80200404, 24)]
        read = plan(outputs, BASE, 0x400000, 'window')
        self.assertEqual(read['window_start'], 0x80200400)
        self.assertEqual(extract(outputs, read), [bytes([1]) * 24])

    def test_extraction_offsets_map_prefix_snapshot(self):
        outputs = [declaration(BASE + 0x404, 12), declaration(BASE + 0x640, 4, 'D')]
        read = plan(outputs, BASE, 0x1000, 'prefix')
        self.assertEqual(extract(outputs, read), [bytes([1]) * 12, bytes([2]) * 4])

    def test_extraction_offsets_map_far_window_snapshot(self):
        outputs = [declaration(FAR, 24)]
        span = (FAR + 24 - BASE + ELEMENT - 1) // ELEMENT * ELEMENT
        read = plan(outputs, BASE, span, 'window')
        self.assertEqual(extract(outputs, read), [bytes([1]) * 24])


class ErrorTests(unittest.TestCase):
    def test_invalid_mode_is_rejected(self):
        outputs = [declaration(BASE, 4)]
        for mode in ('slice', 'PREFIX', '', None, 1, True):
            with self.subTest(mode=mode), self.assertRaisesRegex(
                    WindowError, "must be 'prefix' or 'window'"):
                plan(outputs, BASE, 0x1000, mode)

    def test_empty_or_unknown_outputs_are_rejected(self):
        for outputs in ([], (), None, 'x', dict(address=hex(BASE), nbytes=1), (None,)):
            with self.subTest(outputs=outputs), self.assertRaisesRegex(
                    WindowError, 'No outputs to read|Invalid output'):
                plan(outputs, BASE, 0x1000, 'window')

    def test_invalid_declarations_are_rejected(self):
        bad = [dict(nbytes=4), dict(address=hex(BASE)),
               dict(address=hex(BASE), nbytes=0), dict(address=hex(BASE), nbytes=-4),
               dict(address=hex(BASE), nbytes=True), dict(address=hex(BASE), nbytes=4.0),
               dict(address=hex(BASE), nbytes='4'), dict(address='0x100000000', nbytes=4),
               dict(address='8000', nbytes=4), dict(address=0x80000000, nbytes=4)]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(WindowError):
                plan([item], BASE, 0x1000, 'window')
        # Extra declaration keys are ignored; only address/nbytes matter to plan().
        self.assertEqual(plan([dict(address=hex(BASE), nbytes=4, extra=True)],
                              BASE, 0x1000, 'window')['window_size'], ELEMENT)

    def test_invalid_base_is_rejected(self):
        outputs = [declaration(BASE, 4)]
        for base in (1, 63, BASE + 1, -64, True, '0x80000000', 1.5, None):
            with self.subTest(base=base), self.assertRaisesRegex(
                    WindowError, 'multiple of 64'):
                plan(outputs, base, 0x1000, 'window')

    def test_invalid_span_is_rejected(self):
        outputs = [declaration(BASE, 4)]
        for span in (0, 63, 65, -64, ELEMENT + 1, True, 1.5, None):
            with self.subTest(span=span), self.assertRaisesRegex(
                    WindowError, 'multiple of 64'):
                plan(outputs, BASE, span, 'window')

    def test_window_must_fit_the_4_gib_address_space(self):
        outputs = [declaration(BASE, 4)]
        for base, span in ((0xffffffc0, 128), (BASE, 2 ** 32), (0xffffffc0, 4 * ELEMENT)):
            with self.subTest(base=hex(base), span=span), self.assertRaisesRegex(
                    WindowError, '4 GiB'):
                plan(outputs, base, span, 'window')

    def test_out_of_window_outputs_are_rejected(self):
        base, span = 0x80000000, 0x1000
        for address_value, nbytes in ((base - ELEMENT, 4), (base - 1, 1),
                                      (base + span - 4, 8), (base + span, 1),
                                      (base + 0x800, 0x900)):
            with self.subTest(address=hex(address_value), nbytes=nbytes):
                with self.assertRaisesRegex(WindowError, 'outside the configured physical DDR window'):
                    plan([declaration(address_value, nbytes)], base, span, 'window')
                with self.assertRaises(WindowError):
                    plan([declaration(address_value, nbytes)], base, span, 'prefix')

    def test_plan_does_not_mutate_declarations(self):
        outputs = [declaration(0x80000105, 7), declaration(0x80000084, 3)]
        before = [dict(item) for item in outputs]
        plan(outputs, BASE, 0x1000, 'window')
        plan(tuple(outputs), BASE, 0x1000, 'prefix')
        self.assertEqual(outputs, before)

    def test_describe_reads_reports_both_modes(self):
        reads = describe_reads([declaration(BASE + 0x84, 8)], BASE, 0x1000)
        self.assertEqual(set(reads), {'prefix', 'window'})
        self.assertEqual(reads['prefix']['window_start'], BASE)
        self.assertEqual(reads['window']['window_start'], BASE + 0x80)


if __name__ == '__main__':
    unittest.main()
