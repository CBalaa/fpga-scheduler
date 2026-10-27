/* SPDX-License-Identifier: Apache-2.0
 * UART declarations for daemon DDR capture. Include after the NH UART header.
 * This code declares physical memory; it does not flush caches or wait for DMA.
 * Call on NH after device writes complete, before the UART RESULT record.
 */
#ifndef FPGA_OUTPUT_H
#define FPGA_OUTPUT_H

#include <stddef.h>
#include <stdint.h>

#ifndef FPGA_OUTPUT_PUTS
#define FPGA_OUTPUT_PUTS print_uart
#endif

static inline void fpga_output_u64(uint64_t value, unsigned base) {
  char digits[21];
  unsigned count = 0;
  do {
    unsigned digit = (unsigned)(value % base);
    digits[count++] = (char)(digit < 10 ? '0' + digit : 'a' + digit - 10);
    value /= base;
  } while (value);
  while (count) {
    char text[2] = {digits[--count], 0};
    FPGA_OUTPUT_PUTS(text);
  }
}

/* Dense row-major INT32 only. Offsets and strides are measured in elements.
 * Return -1 without emitting any record on invalid name/layout/overflow.
 * Memory must stay unchanged until the daemon has captured the completed task.
 */
static inline int fpga_export_i32_2d(const char *name, const void *aligned,
                                   int64_t offset, int64_t rows, int64_t columns,
                                   int64_t row_stride, int64_t column_stride) {
  if (!name || !aligned || offset < 0 || rows <= 0 || columns <= 0 ||
      row_stride != columns || column_stride != 1)
    return -1;
  size_t count = 0;
  for (; name[count]; ++count) {
    char c = name[count];
    int alpha = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
    if (count >= 64 || (!alpha && (count == 0 ||
        !((c >= '0' && c <= '9') || c == '_' || c == '-'))))
      return -1;
  }
  if (!count || (uint64_t)rows > UINT64_MAX / (uint64_t)columns)
    return -1;
  uint64_t elements = (uint64_t)rows * (uint64_t)columns;
  if (elements > 16777216u / sizeof(int32_t) ||
      (uint64_t)offset > UINTPTR_MAX / sizeof(int32_t))
    return -1;
  uintptr_t address = (uintptr_t)aligned;
  uintptr_t displacement = (uintptr_t)offset * sizeof(int32_t);
  uint64_t bytes = elements * sizeof(int32_t);
  if (displacement > UINTPTR_MAX - address)
    return -1;
  address += displacement;
  if (bytes > UINTPTR_MAX - address)
    return -1;
  FPGA_OUTPUT_PUTS("\r\n@@FPGA_OUTPUT {\"version\":1,\"name\":\"");
  FPGA_OUTPUT_PUTS(name);
  FPGA_OUTPUT_PUTS("\",\"address\":\"0x");
  fpga_output_u64(address, 16);
  FPGA_OUTPUT_PUTS("\",\"nbytes\":");
  fpga_output_u64(bytes, 10);
  FPGA_OUTPUT_PUTS(",\"dtype\":\"<i4\",\"shape\":[");
  fpga_output_u64((uint64_t)rows, 10);
  FPGA_OUTPUT_PUTS(",");
  fpga_output_u64((uint64_t)columns, 10);
  FPGA_OUTPUT_PUTS("]}\r\n");
  return 0;
}

/* Accepts the application's existing MLIR-style MemRef2 type.
 * Pass a descriptor pointer without side effects, e.g. &c.
 */
#define fpga_export_memref2_i32(name, descriptor)                              \
  fpga_export_i32_2d((name), (descriptor)->aligned, (descriptor)->offset,      \
                    (descriptor)->sizes[0], (descriptor)->sizes[1],           \
                    (descriptor)->strides[0], (descriptor)->strides[1])

#endif
