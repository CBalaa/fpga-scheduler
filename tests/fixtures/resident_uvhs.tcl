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
proc topology {args} {
    record topology {*}$args
    set ports {0 1 2 3 4 5 6 7}
    if {[info exists ::env(FAKE_FPGA_PORTS)]} {set ports $::env(FAKE_FPGA_PORTS)}
    set runtime_ports $ports
    if {[info exists ::env(FAKE_TOPOLOGY_PORTS)]} {set ports $::env(FAKE_TOPOLOGY_PORTS)}
    puts "Topology Data"
    puts [string repeat - 201]
    if {[lsearch $args -fpgas] >= 0} {
        set row_format "%-17s%-22s%-22s%-18s%-17s%-17s%s"
        puts [format $row_format "System Name" "Topology Board ID" "Topology Fpga Id" "Mapped Board ID" "Device Type" "Used by Host" "Used by User"]
        puts [string repeat - 129]
        if {[info exists ::env(FAKE_TOPOLOGY_FPGA_ROWS)]} {puts $::env(FAKE_TOPOLOGY_FPGA_ROWS); return}
        foreach p $ports {
            puts [format $row_format uvhs-0 "TB[expr {$p / 4 + 1}]" "F[expr {$p % 4}]" "" V1 "" ""]
        }
    } else {
        set row_format "%-17s%-22s%-17s%-35s%-27s%-18s%-17s%-17s%s"
        puts [format $row_format "System Name" "Topology Board ID" "Device Type" "HW ID CODE" "HW Location" "Mapped Board ID" "Used by Host" "Used by User" Duration]
        puts [string repeat - 201]
        if {[info exists ::env(FAKE_TOPOLOGY_BOARD_ROWS)]} {puts $::env(FAKE_TOPOLOGY_BOARD_ROWS); return}
        set boards {}
        foreach p $ports {
            set board [expr {$p / 4}]
            if {[lsearch $boards $board] >= 0} {continue}
            lappend boards $board
            set mapped ""
            foreach runtime_port $runtime_ports {
                if {$runtime_port / 4 == $board} {set mapped "B$board"; break}
            }
            set user ""
            if {[info exists ::env(FAKE_TOPOLOGY_BOARD_USER)]} {set user $::env(FAKE_TOPOLOGY_BOARD_USER)}
            puts [format $row_format uvhs-0 "TB[expr {$board + 1}]" V1 "SIMULATED-BOARD-$board" "" $mapped "" $user "0s"]
        }
    }
    puts ""
}
proc query {args} {
    global reset_value
    record query {*}$args
    switch -- [lindex $args 0] {
        -fpgas {
            set topology [expr {[lsearch $args -tb] >= 0}]
            puts "Installed DUT FPGA Status:"
            puts "System Name      Board   FPGA    Power   Status                Owner                 Booked"
            puts [string repeat - 97]
            set rows_key [expr {$topology ? "FAKE_TOPOLOGY_ROWS" : "FAKE_FPGA_ROWS"}]
            if {[info exists ::env($rows_key)]} {puts $::env($rows_key); return}
            set ports {0 1 2 3 4 5 6 7}
            if {[info exists ::env(FAKE_FPGA_PORTS)]} {set ports $::env(FAKE_FPGA_PORTS)}
            if {$topology && [info exists ::env(FAKE_TOPOLOGY_PORTS)]} {
                set ports $::env(FAKE_TOPOLOGY_PORTS)
            }
            foreach p $ports {
                if {$topology} {set board "TB[expr {$p / 4 + 1}]"} else {set board "B[expr {$p / 4}]"}
                puts "uvhs-0 $board F[expr {$p % 4}] on link down false"
            }
        }
        -clock {
            set topology [expr {[lsearch $args -tb] >= 0}]
            set rows_key [expr {$topology ? "FAKE_TOPOLOGY_CLOCK_ROWS" : "FAKE_CLOCK_ROWS"}]
            if {[info exists ::env($rows_key)]} {puts $::env($rows_key); return}
            if {$topology} {
                puts "TB1 6 5.529600 NA PLL"; puts "TB2 6 5.529600 NA PLL"
            } else {puts "B0 6 5.529600 NA PLL"; puts "B1 6 5.529600 NA PLL"}
        }
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
    global loaded work
    record readmem {*}$args
    set target [lindex $args [expr {[lsearch $args -file] + 1}]]
    set rtl [lindex $args [expr {[lsearch $args -rtl] + 1}]]
    if {[info exists ::env(FAKE_READ_DELAY)]} {
        set f [open [file join $work reading] w]; puts $f reading; close $f
        after $::env(FAKE_READ_DELAY)
    }
    if {[info exists ::env(FAKE_READBACK_ERROR)]} {
        set f [open $target w]; puts $f corrupt; close $f
    } elseif {[regexp {\[(\d+):(\d+)\]} $rtl unused high low]} {
        # Model the 512-bit i_ddr element array: element N holds bytes [N*64, N*64+64).
        set f [open $loaded rb]
        set data [read $f [expr {($high + 1) * 64}]]
        close $f
        set f [open $target wb]
        puts -nonewline $f [string range $data [expr {$low * 64}] end]
        close $f
    } else {file copy -force $loaded $target}
}
while {[gets stdin line] >= 0} {
    if {[catch {uplevel #0 $line} value]} {puts stderr "ERROR: $value"}
    flush stdout
}
