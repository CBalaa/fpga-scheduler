# Standalone BOSC toolchains. Individual RISCV_* commands override either family.
TOOLCHAIN ?= llvm
LLVM_BIN ?=
GCC_BIN ?=
CROSS_COMPILE ?= riscv64-unknown-elf-
GCC_PREFIX ?= $(CROSS_COMPILE)

fpga_llvm_tool = $(if $(and $(LLVM_BIN),$(wildcard $(LLVM_BIN)/$(1))),$(LLVM_BIN)/$(1),$(1))
fpga_gcc_tool = $(if $(GCC_BIN),$(patsubst %/,%,$(GCC_BIN))/,)$(GCC_PREFIX)$(1)

ifeq ($(TOOLCHAIN),gcc)
RISCV_CC ?= $(call fpga_gcc_tool,gcc)
RISCV_LD ?= $(call fpga_gcc_tool,ld) -m elf64lriscv
RISCV_OBJCOPY ?= $(call fpga_gcc_tool,objcopy)
RISCV_OBJDUMP ?= $(call fpga_gcc_tool,objdump)
else ifeq ($(TOOLCHAIN),llvm)
RISCV_CC ?= $(call fpga_llvm_tool,clang) --target=riscv64-unknown-elf
RISCV_LD ?= $(call fpga_llvm_tool,ld.lld) -m elf64lriscv
RISCV_OBJCOPY ?= $(call fpga_llvm_tool,llvm-objcopy)
RISCV_OBJDUMP ?= $(call fpga_llvm_tool,llvm-objdump)
else
$(error TOOLCHAIN must be gcc or llvm)
endif
