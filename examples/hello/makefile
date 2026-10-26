# ===- makefile - Standalone NH hello for the FPGA scheduler -------------------
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Adapted from buddy-mlir-for-fpga/examples/BOSCAMEFPGA/hello/makefile.
# All program sources are in this repository. Select GNU or LLVM tools with
# TOOLCHAIN. The default build includes the scheduler UART completion protocol.

.DEFAULT_GOAL := all
.PHONY: all clean dump help size FORCE
.DELETE_ON_ERROR:

RUNTIME_DIR := $(abspath ../../runtime/bosc)
include $(RUNTIME_DIR)/toolchain.mk

VARIANT ?= hello
ifeq ($(filter $(VARIANT),hello exit7 trap hang),)
$(error VARIANT must be hello, exit7, trap or hang)
endif
BUILD_DIR ?= $(if $(filter hello,$(VARIANT)),build,build/$(VARIANT))
UART_FREQ_HZ ?= 5529600
UART_BAUD ?= 115200
EXTRA_OBJECTS ?=

VARIANT_CFLAGS :=
VARIANT_LDFLAGS :=
ifeq ($(VARIANT),exit7)
VARIANT_CFLAGS := -DFPGA_TEST_EXIT=7
else ifeq ($(VARIANT),trap)
VARIANT_CFLAGS := -DFPGA_TEST_TRAP
else ifeq ($(VARIANT),hang)
VARIANT_CFLAGS := -DFPGA_TEST_HANG
endif
ifneq ($(VARIANT),hello)
VARIANT_LDFLAGS := --wrap=main
endif

LD_SCRIPT := platform/hello_nr.ld
BIN := $(BUILD_DIR)/hello.bin
OBJECTS := $(addprefix $(BUILD_DIR)/,crt_uart.o hello.o bare_runtime.o uart.o protocol.o)
ifneq ($(VARIANT),hello)
OBJECTS += $(BUILD_DIR)/main_faults.o
endif

RISCV_CFLAGS := -march=rv64gc -mabi=lp64d -mcmodel=medany -nostdlib \
                -fno-builtin -ffreestanding -fno-pie -O2 \
                -ffunction-sections -fdata-sections -MMD -MP \
                -DUART_FREQ_HZ=$(UART_FREQ_HZ)u -DUART_BAUD=$(UART_BAUD)u \
                -I$(RUNTIME_DIR) $(EXTRA_CFLAGS)
PROTOCOL_CFLAGS := -march=rv64gc -mabi=lp64d -mcmodel=medany -fno-pie -O2 \
                   -ffreestanding -fno-builtin -Wall -Wextra -Werror -MMD -MP \
                   -I$(RUNTIME_DIR) $(EXTRA_CFLAGS)

all: $(BIN)

# Update the stamp only when the toolchain, clock, flags or variant changes.
# This also makes reusing BUILD_DIR across variants safe.
$(BUILD_DIR)/build.config: FORCE | $(BUILD_DIR)
	@$(file >$(BUILD_DIR)/build.config.tmp,$(TOOLCHAIN) | $(RISCV_CC) | $(RISCV_LD) | $(RISCV_OBJCOPY) | $(RISCV_CFLAGS) | $(PROTOCOL_CFLAGS) | $(VARIANT_CFLAGS) | $(VARIANT_LDFLAGS) | $(EXTRA_LDFLAGS) | $(EXTRA_OBJECTS))
	@cmp -s $@ $@.tmp && rm $@.tmp || mv $@.tmp $@

$(BUILD_DIR)/hello.o: hello.c $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(RISCV_CFLAGS) -c $< -o $@

$(BUILD_DIR)/bare_runtime.o: $(RUNTIME_DIR)/bare_runtime.c $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(RISCV_CFLAGS) -c $< -o $@

$(BUILD_DIR)/crt_uart.o: $(RUNTIME_DIR)/crt_uart.S $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(RISCV_CFLAGS) -c $< -o $@

$(BUILD_DIR)/uart.o: $(RUNTIME_DIR)/uart.c $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(RISCV_CFLAGS) -c $< -o $@

$(BUILD_DIR)/protocol.o: $(RUNTIME_DIR)/uart_protocol.c $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(PROTOCOL_CFLAGS) -c $< -o $@

$(BUILD_DIR)/main_faults.o: ../../tests/fixtures/bosc_main_faults.c $(BUILD_DIR)/build.config $(MAKEFILE_LIST) | $(BUILD_DIR)
	$(RISCV_CC) $(PROTOCOL_CFLAGS) $(VARIANT_CFLAGS) -c $< -o $@

$(BUILD_DIR)/hello.elf: $(OBJECTS) $(EXTRA_OBJECTS) $(LD_SCRIPT) $(BUILD_DIR)/build.config $(MAKEFILE_LIST)
	$(RISCV_LD) --script=$(LD_SCRIPT) --gc-sections $(VARIANT_LDFLAGS) $(EXTRA_LDFLAGS) -o $@ $(OBJECTS) $(EXTRA_OBJECTS)

$(BIN): $(BUILD_DIR)/hello.elf $(BUILD_DIR)/build.config
	$(RISCV_OBJCOPY) -O binary $< $@

size: $(BIN)
	@echo "hello.bin: $$(stat -c%s $(BIN)) bytes (raw), NH entry: 0x80000000"

dump: $(BUILD_DIR)/hello.elf
	$(RISCV_OBJDUMP) -h -d $<

clean:
	rm -rf $(BUILD_DIR)

help:
	@echo "make all size       Build scheduler-ready NH hello (UART 5.5296 MHz / 115200)"
	@echo "make TOOLCHAIN=gcc  Use RISC-V GNU gcc/as, ld and objcopy"
	@echo "make TOOLCHAIN=llvm Use clang, ld.lld and llvm-objcopy (default)"
	@echo "make dump           Disassemble the ELF"
	@echo "make clean          Remove the selected BUILD_DIR"
	@echo "make VARIANT=exit7  Build an explicit acceptance fault variant (also trap/hang)"
	@echo "Overrides: BUILD_DIR, GCC_BIN, GCC_PREFIX/CROSS_COMPILE, LLVM_BIN, RISCV_*, UART_FREQ_HZ, UART_BAUD, EXTRA_*"

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

-include $(OBJECTS:.o=.d)
