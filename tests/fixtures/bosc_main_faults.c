/* Fault injection for live acceptance builds, linked with --wrap=main.
 * The actual hello main still executes, and the runtime captures the wrapper's
 * real return/trap/hang. This fixture is linked only for explicit fault variants.
 */
#if defined(FPGA_TEST_EXIT) || defined(FPGA_TEST_TRAP) || defined(FPGA_TEST_HANG)
extern int __real_main(void);
int __wrap_main(void) {
  int status = __real_main();
#if defined(FPGA_TEST_EXIT)
  (void)status;
  return FPGA_TEST_EXIT;
#elif defined(FPGA_TEST_TRAP)
  __asm__ volatile(".word 0");
  return status;
#else
  (void)status;
  for (;;)
    __asm__ volatile("wfi");
#endif
}
#endif
