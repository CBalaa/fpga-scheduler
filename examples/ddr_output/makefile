# SPDX-License-Identifier: Apache-2.0
.DEFAULT_GOAL := all
.PHONY: all clean FORCE
.DELETE_ON_ERROR:
RUNTIME := $(abspath ../../runtime/bosc)
include $(RUNTIME)/toolchain.mk
BUILD_DIR ?= build
CFLAGS := -march=rv64gc -mabi=lp64d -mcmodel=medany -nostdlib \
          -ffreestanding -fno-builtin -fno-pie -O2 -Wall -Wextra -Werror \
          -ffunction-sections -fdata-sections -MMD -MP \
          -I$(RUNTIME) -I../../runtime/include $(EXTRA_CFLAGS)
OBJECTS := $(addprefix $(BUILD_DIR)/,output.o crt_uart.o bare_runtime.o uart.o uart_protocol.o)
all: $(BUILD_DIR)/output.bin
$(BUILD_DIR):
	mkdir -p $@
$(BUILD_DIR)/build.config: FORCE | $(BUILD_DIR)
	@$(file >$(BUILD_DIR)/build.config.tmp,$(RISCV_CC) | $(RISCV_LD) | $(RISCV_OBJCOPY) | $(CFLAGS))
	@cmp -s $@ $@.tmp && rm $@.tmp || mv $@.tmp $@
$(BUILD_DIR)/output.o: output.c $(BUILD_DIR)/build.config | $(BUILD_DIR)
	$(RISCV_CC) $(CFLAGS) -c $< -o $@
$(BUILD_DIR)/%.o: $(RUNTIME)/%.c $(BUILD_DIR)/build.config | $(BUILD_DIR)
	$(RISCV_CC) $(CFLAGS) -c $< -o $@
$(BUILD_DIR)/crt_uart.o: $(RUNTIME)/crt_uart.S $(BUILD_DIR)/build.config | $(BUILD_DIR)
	$(RISCV_CC) $(CFLAGS) -c $< -o $@
$(BUILD_DIR)/output.elf: $(OBJECTS) ../hello/platform/hello_nr.ld
	$(RISCV_LD) --script=../hello/platform/hello_nr.ld --gc-sections -o $@ $(OBJECTS)
$(BUILD_DIR)/output.bin: $(BUILD_DIR)/output.elf
	$(RISCV_OBJCOPY) -O binary $< $@
clean:
	rm -rf $(BUILD_DIR)
-include $(OBJECTS:.o=.d)
