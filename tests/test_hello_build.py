"""Compile the standalone sources with real GNU tools; never contact hardware."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
GNU = {name: shutil.which('riscv64-unknown-elf-' + name)
       for name in ('gcc', 'ld', 'objcopy', 'objdump')}
LLVM = {name: shutil.which(name) for name in ('clang', 'ld.lld', 'llvm-objcopy', 'llvm-objdump')}


@unittest.skipUnless(shutil.which('make') and all(GNU.values()),
                     'RISC-V bare-metal GCC/binutils are not installed')
class GNUHelloBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fpga-hello-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Copy only in-repository build inputs, with no Buddy workspace or old objects.
        for directory in ('examples/hello', 'runtime/bosc', 'tests/fixtures'):
            shutil.copytree(PROJECT / directory, self.root / directory,
                            ignore=shutil.ignore_patterns('build', '__pycache__'))
        (self.root / 'tools').mkdir()
        shutil.copy2(PROJECT / 'tools/build_hello.py', self.root / 'tools')
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('RISCV_') and key not in
                    ('TOOLCHAIN', 'GCC_BIN', 'GCC_PREFIX', 'CROSS_COMPILE',
                     'LLVM_BIN', 'MAKEFLAGS', 'MFLAGS')}
        self.poison = self.root / 'no-llvm'
        self.poison.mkdir()
        for name in LLVM:
            tool = self.poison / name
            tool.write_text('#!/bin/sh\necho "Unexpected LLVM invocation" >&2\nexit 99\n')
            tool.chmod(0o755)
        self.gnu_env = dict(self.env, PATH=str(self.poison) + os.pathsep + self.env['PATH'])

    def make(self, *variables, build='build', targets=('all', 'dump'), env=None):
        command = ['make', '-C', str(self.root / 'examples/hello'),
                   'BUILD_DIR=' + str(self.root / build), *variables, *targets]
        result = subprocess.run(command, env=self.gnu_env if env is None else env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def assert_image(self, build, variant='hello'):
        directory = self.root / build
        elf = (directory / 'hello.elf').read_bytes()
        image = (directory / 'hello.bin').read_bytes()
        self.assertEqual(elf[:6], b'\x7fELF\x02\x01')  # ELF64 little endian
        self.assertEqual(struct.unpack_from('<H', elf, 18)[0], 243)  # EM_RISCV
        self.assertEqual(struct.unpack_from('<Q', elf, 24)[0], 0x80000000)
        self.assertEqual(struct.unpack_from('<I', elf, 48)[0] & 7, 5)  # RVC + lp64d
        self.assertGreater(len(image), 0)
        self.assertLess(len(image), 65536)  # No NOLOAD stack in the raw image.
        table = subprocess.check_output([GNU['objdump'], '-t', str(directory / 'hello.elf')],
                                        text=True)
        symbols = {}
        for line in table.splitlines():
            fields = line.split()
            if len(fields) >= 4:
                try:
                    symbols[fields[-1]] = int(fields[0], 16)
                except ValueError:
                    pass
        self.assertEqual(symbols['_start'], 0x80000000)
        self.assertEqual(symbols['__stack_top'] - symbols['__stack_base'], 1024 * 1024)
        self.assertEqual(symbols['__stack_top'] % 16, 0)
        for start, end in (('__bss_start', '__bss_end'), ('__tbss_start', '__tbss_end')):
            self.assertLessEqual(symbols[start], symbols[end])
            self.assertEqual(symbols[start] % 8, 0)
            self.assertEqual(symbols[end] % 8, 0)
        self.assertIn('__global_pointer$', symbols)
        self.assertEqual('__wrap_main' in symbols, variant != 'hello')

    def custom_tools(self):
        directory = self.root / 'gnu-bin'
        directory.mkdir()
        for name, target in GNU.items():
            (directory / ('bare-' + name)).symlink_to(target)
        return directory

    def test_all_variants_build_with_llvm_unavailable(self):
        for variant in ('hello', 'exit7', 'trap', 'hang'):
            with self.subTest(variant=variant):
                self.make('TOOLCHAIN=gcc', 'VARIANT=' + variant, build=variant)
                self.assert_image(variant, variant)

    def test_gnu_directory_prefix_cross_compile_and_command_overrides(self):
        directory = self.custom_tools()
        configurations = (
            ('GCC_BIN=' + str(directory), 'GCC_PREFIX=bare-'),
            ('GCC_PREFIX=' + str(directory / 'bare-'),),
            ('CROSS_COMPILE=' + str(directory / 'bare-'),),
            ('GCC_BIN=/not-installed', 'GCC_PREFIX=not-installed-',
             'RISCV_CC=' + GNU['gcc'], 'RISCV_LD=' + GNU['ld'] + ' -m elf64lriscv',
             'RISCV_OBJCOPY=' + GNU['objcopy'], 'RISCV_OBJDUMP=' + GNU['objdump']),
        )
        for index, configuration in enumerate(configurations):
            with self.subTest(configuration=configuration):
                build = 'custom-' + str(index)
                self.make('TOOLCHAIN=gcc', *configuration, build=build)
                self.assert_image(build)

    def test_build_helper_accepts_gnu_options_and_records_image_identity(self):
        directory = self.custom_tools()
        result = subprocess.run([sys.executable, str(self.root / 'tools/build_hello.py'),
                                 '--toolchain', 'gcc', '--gcc-bin', str(directory),
                                 '--gcc-prefix', 'bare-', '--variant', 'exit7',
                                 '--output', str(self.root / 'images')], env=self.gnu_env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_image('images/exit7', 'exit7')
        manifest = json.loads((self.root / 'images/exit7/manifest.json').read_text())
        image = (self.root / 'images/exit7/hello.bin').read_bytes()
        self.assertEqual(manifest['toolchain'], 'gcc')
        self.assertEqual(manifest['variant'], 'exit7')
        self.assertEqual(manifest['sha256'], hashlib.sha256(image).hexdigest())
        self.assertEqual(manifest['bytes'], len(image))

    @unittest.skipUnless(all(LLVM.values()), 'Optional LLVM tools are not installed')
    def test_default_llvm_and_switching_rebuild_in_same_directory(self):
        directory = self.root / 'llvm-bin'
        directory.mkdir()
        for name, target in LLVM.items():
            (directory / name).symlink_to(target)
        llvm_option = 'LLVM_BIN=' + str(directory)
        output = self.make(llvm_option, env=self.env)
        self.assertIn('clang', output)
        self.assertTrue((self.root / 'build/build.config').read_text().startswith('llvm |'))
        self.assert_image('build')
        output = self.make('TOOLCHAIN=gcc', 'VARIANT=exit7')
        self.assertIn('riscv64-unknown-elf-gcc', output)
        self.assert_image('build', 'exit7')
        output = self.make('TOOLCHAIN=llvm', llvm_option, env=self.env)
        self.assertIn('clang', output)
        self.assert_image('build')
        self.make('TOOLCHAIN=gcc')
        self.assert_image('build')
