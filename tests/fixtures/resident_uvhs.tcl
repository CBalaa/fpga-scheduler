# A fake UVHS with real Tcl catch/source semantics and a PTY control channel.
set work [lindex $argv [expr {[lsearch $argv -workdir] + 1}]]
set loaded ""
set reset_value 1
set allocated ""
proc record {name args} {
    set f [open [file join $::work control-commands] a]
    puts $f [join [concat [list $name] $args] " "]
    close $f
}
proc allocate {args} {
    record allocate {*}$args
    if {[info exists ::env(FAKE_REJECT_ALLOCATION)]} {error "explicit allocation incompatible with current server mode"}
    set ::allocated [lindex $args 1]
}
proc load_db {args} {
    record load_db {*}$args
    # Model both observed server flows without rewriting their command order.
    if {[lindex $args 0] eq "-release"} {return}
    set to [lsearch $args -to]
    if {$::allocated ne "" && $to >= 0} {
        error "Load_DB not support -to after allocate get invalid parameter"
    }
    if {[info exists ::env(FAKE_REQUIRE_ALLOCATION)] && $::allocated eq ""} {error "explicit allocation required"}
    if {$to < 0 && $::allocated eq ""} {error "missing target allocation"}
    if {![file isdirectory ./hw.dat]} {error "relative ./hw.dat did not resolve in isolated workdir"}
}
proc set_option {args} {record set_option {*}$args}
proc config {args} {
    global work
    record config {*}$args
    if {[lindex $args 0] eq "-clock"} {
        set f [open [file join $work clock-configs] a]; puts $f $args; close $f
    }
}
proc initialize {} {
    record initialize
    if {[info exists ::env(FAKE_INIT_DELAY)]} {after $::env(FAKE_INIT_DELAY)}
    if {[info exists ::env(FAKE_INIT_ERROR)]} {error "injected initialization failure"}
    if {[info exists ::env(FAKE_INIT_REPORTED_ERROR)]} {puts "ERROR: vendor initialization failed"}
    if {[info exists ::env(FAKE_INIT_RELEASE_CPU)]} {set ::reset_value 0}
}
proc download {} {
    global work
    record download
    set f [open [file join $work downloads] a]; puts $f download; close $f
}
proc query {args} {
    global reset_value
    record query {*}$args
    switch -- [lindex $args 0] {
        -fpgas {
            set ports {0 1 2 3 4 5 6 7}
            if {[info exists ::env(FAKE_FPGA_PORTS)]} {set ports $::env(FAKE_FPGA_PORTS)}
            foreach p $ports {puts "uvhs-0 B[expr {$p / 4}] F[expr {$p % 4}] on link down false"}
        }
        -clock {puts "B0 6 5.529600 NA PLL"; puts "B1 6 5.529600 NA PLL"}
        -reset {
            if {[info exists ::env(FAKE_RESET_QUERY_ZERO)]} {puts "Command: reset -name cpu_reset -value 1"; puts "0 1 cpu_reset 0"} else {puts "cpu_reset $reset_value"}
        }
    }
}
proc reset {args} {
    global reset_value work loaded
    record reset {*}$args
    set reset_value [lindex $args end]
    if {$reset_value == 0} {
        set f [open $loaded r]; set mode [read $f 40]; close $f
        set f [open [file join $work release] a]; puts $f [string trimright $mode \x00]; close $f
    }
    if {$reset_value == 1 && [info exists ::env(FAKE_RESET_ERROR)]} {error "injected reset failure"}
}
proc writemem {args} {
    global loaded work
    record writemem {*}$args
    set loaded [lindex $args [expr {[lsearch $args -file] + 1}]]
    if {[info exists ::env(FAKE_LOAD_DELAY)]} {
        set f [open [file join $work loading] w]; puts $f loading; close $f
        after $::env(FAKE_LOAD_DELAY)
    }
    if {[info exists ::env(FAKE_COMMAND_ERROR)]} {error "injected command error"}
}
proc readmem {args} {
    global loaded
    record readmem {*}$args
    set target [lindex $args [expr {[lsearch $args -file] + 1}]]
    if {[info exists ::env(FAKE_READBACK_ERROR)]} {
        set f [open $target w]; puts $f corrupt; close $f
    } else {file copy -force $loaded $target}
}
while {[gets stdin line] >= 0} {
    if {[catch {uplevel #0 $line} value]} {puts stderr "ERROR: $value"}
    flush stdout
}
