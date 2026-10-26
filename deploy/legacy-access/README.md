# Optional BOSC access VM/VPN templates

The files in this directory are adapted Apache-2.0 reference scripts from
buddy-caas 34268ca6 (see repository NOTICE). They support a QEMU VM → UniVPN
route and are not installed or started by the scheduler. Deployment paths,
VPN profiles and backend endpoints must be configured for the target host.

- vm.py and fpga-access-vm.service supervise a pre-provisioned QEMU guest.
- vpn.py and fpga-access-vpn.service supervise a separately installed UniVPN client.
- The VPN supervisor requires pexpect in that VM (not a scheduler dependency).
- The systemd VPN unit reads a root-owned, mode-600 credential file through
  LoadCredential. Never put that file in this repository or a client image.
- Authentication failures stop login retries; inspect and repair credentials
  before restarting. Log output must not expose VPN credentials.

The VM user unit expects vm.py under `$HOME/.local/lib/fpga-scheduler/`.
VM state defaults to `$HOME/.local/share/fpga-scheduler/access-vm`; override it
with `FPGA_ACCESS_VM_ROOT` in `$HOME/.config/fpga-scheduler/access-vm.env`.
Provision `disk.qcow2` and `seed.iso` there separately. SSH forwarding binds
only to loopback; guest output is stored with the VM state, and service output
goes to the journal.

The VPN system unit expects vpn.py under `/usr/local/lib/fpga-scheduler/`.
Its external `/etc/fpga-scheduler/vpn.json` must contain:

| Field | Purpose |
|---|---|
| `username`, `password` | VPN credentials supplied through systemd LoadCredential |
| `profile` | Exact VPN menu profile name |
| `profile_index` | Positive integer identifying that menu entry |
| `probe_hosts` | Nonempty list of backend hostnames whose SSH ports establish readiness |

`UniVPNCS` is found on PATH, or selected by `FPGA_VPN_CLIENT` in
`/etc/fpga-scheduler/access-vpn.env`. Keep credentials and actual endpoint
configuration outside this repository; no profile or backend address is built in.
Invalid configuration stops automatic login retries with exit status 78.

For new deployments, establish the network route separately and use the
scheduler's configurable OpenSSH adapter. Configure a ProxyJump alias when the
access VM is the jump host. If a JSON relay is necessary, use the standalone
src/fpga_scheduler/remote/relay.py with an explicit allowed-node JSON file,
not the old installation script. There is no automatic VPN login, VM creation,
SSH key installation, service installation, or hardware deployment command in
this repository's default startup.

These legacy supervisors have not been tested against a live VM here. Adapt
paths and endpoints deliberately, and validate each service on its target host.
