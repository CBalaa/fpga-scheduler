# A fake UVHS with real Tcl catch/source semantics and a PTY control channel.
set work [lindex $argv [expr {[lsearch $argv -workdir] + 1}]]
set loaded ""
set reset_value 1
proc load_db {args} {}
proc set_option {args} {}
proc config {args} {
    global work
    if {[lindex $args 0] eq "-clock"} {
        set f [open [file join $work clock-configs] a]; puts $f $args; close $f
    }
}
proc initialize {} {}
proc download {} {
    global work
    set f [open [file join $work downloads] a]; puts $f download; close $f
}
proc query {args} {
    global reset_value
    switch -- [lindex $args 0] {
        -fpgas {for {set p 0} {$p < 8} {incr p} {puts "uvhs-0 B[expr {$p / 4}] F[expr {$p % 4}] on link down false"}}
        -clock {puts "B0 6 5.529600 NA PLL"; puts "B1 6 5.529600 NA PLL"}
        -reset {
            if {[info exists ::env(FAKE_RESET_QUERY_ZERO)]} {puts "Command: reset -name cpu_reset -value 1"; puts "0 1 cpu_reset 0"} else {puts "cpu_reset $reset_value"}
        }
    }
}
proc reset {args} {
    global reset_value work loaded
    set reset_value [lindex $args end]
    if {$reset_value == 0} {
        set f [open $loaded r]; set mode [read $f 40]; close $f
        set f [open [file join $work release] a]; puts $f [string trimright $mode \x00]; close $f
    }
    if {$reset_value == 1 && [info exists ::env(FAKE_RESET_ERROR)]} {error "injected reset failure"}
}
proc writemem {args} {
    global loaded work
    set loaded [lindex $args [expr {[lsearch $args -file] + 1}]]
    if {[info exists ::env(FAKE_LOAD_DELAY)]} {
        set f [open [file join $work loading] w]; puts $f loading; close $f
        after $::env(FAKE_LOAD_DELAY)
    }
    if {[info exists ::env(FAKE_COMMAND_ERROR)]} {error "injected command error"}
}
proc readmem {args} {
    global loaded
    set target [lindex $args [expr {[lsearch $args -file] + 1}]]
    if {[info exists ::env(FAKE_READBACK_ERROR)]} {
        set f [open $target w]; puts $f corrupt; close $f
    } else {file copy -force $loaded $target}
}
while {[gets stdin line] >= 0} {
    if {[catch {uplevel #0 $line} value]} {puts stderr "ERROR: $value"}
    flush stdout
}
