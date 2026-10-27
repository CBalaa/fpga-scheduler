# Software-only UVHS command surface. Invoked only by resident-sim.
# Commands still travel through the production numbered Tcl catch/ack protocol.
set work [lindex $argv [expr {[lsearch $argv -workdir] + 1}]]
set loaded ""
set reset_value 1
set released 0
set sim_mode hello
set sim_duration 0
proc load_db {args} {}
proc set_option {args} {}
proc config {args} {}
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
        -reset {puts "cpu_reset $reset_value"}
    }
}
proc reset {args} {
    global reset_value work loaded released sim_mode
    set requested [lindex $args end]
    if {$requested == 1 && $released && $sim_mode eq "cleanup-error"} {error "simulated reset failure"}
    set reset_value $requested
    if {$reset_value == 0} {
        if {$sim_mode eq "uvhs-exit"} {exit 3}
        set f [open [file join $work running.tmp] w]; puts $f $loaded; close $f
        file rename -force [file join $work running.tmp] [file join $work running]
        set released 1
    } else {file delete -force [file join $work running]}
}
proc writemem {args} {
    global loaded sim_mode sim_duration
    set loaded [lindex $args [expr {[lsearch $args -file] + 1}]]
    source [file join [file dirname $loaded] sim.tcl]
    if {$sim_mode eq "command-error"} {error "simulated DDR write error"}
    if {$sim_mode eq "command-timeout"} {after [expr {int($sim_duration * 1000)}]}
}
proc readmem {args} {
    global loaded sim_mode
    set target [lindex $args [expr {[lsearch $args -file] + 1}]]
    if {$sim_mode eq "readback-error"} {
        set f [open $target w]; puts $f corrupt; close $f
    } else {file copy -force $loaded $target}
}
while {[gets stdin line] >= 0} {
    if {[catch {uplevel #0 $line} value]} {puts stderr "ERROR: $value"}
    flush stdout
}
