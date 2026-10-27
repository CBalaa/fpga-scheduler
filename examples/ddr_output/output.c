/* SPDX-License-Identifier: Apache-2.0
 * NH-only: compute a 4x8 INT32 matrix and export its actual DDR bytes.
 */
#include "uart.h"
#include "fpga_output.h"

typedef struct {
  void *allocated, *aligned;
  int64_t offset, sizes[2], strides[2];
} MemRef2;

/* Keep a nonzero offset and an unaligned end to exercise exact byte slicing.
 * The extra cells are guards, not part of the exported matrix.
 */
static int32_t storage[4 * 8 + 2] __attribute__((aligned(64)));

int main(void) {
  storage[0] = 0x12345678;
  storage[33] = 0x76543210;
  for (int i = 0; i < 4; ++i)
    for (int j = 0; j < 8; ++j)
      storage[1 + i * 8 + j] = i * 100 - j * 7;

  /* NH supports Zicbom. Write back dirty lines before external DDR readback.
   * This is a platform operation, deliberately separate from the export API.
   * E6/AME callers must use their own validated completion/cache protocol.
   */
  __asm__ volatile("fence rw, rw" ::: "memory");
  for (uintptr_t p = (uintptr_t)storage;
       p < (uintptr_t)storage + sizeof(storage); p += 64)
    /* Encode cbo.flush for GNU toolchains predating the Zicbom mnemonic. */
    __asm__ volatile(".insn i 0x0f, 2, x0, %0, 2" :: "r"(p) : "memory");
  __asm__ volatile("fence rw, rw" ::: "memory");

  MemRef2 c = {storage, storage, 1, {4, 8}, {8, 1}};
  print_uart("Computed C[4,8]; validation runs on host.\r\n");
  return fpga_export_memref2_i32("C", &c) == 0 ? 0 : 1;
}
