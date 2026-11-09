# terminal-vm

One command and one MCP server for computer-use agents: a separate desktop VM, viewed and controlled from a terminal, with the same tools everywhere (`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `status`).

| Host | Linux guests | Windows guests | macOS guests |
|---|---|---|---|
| Linux (QEMU + KVM) | yes | yes | no |
| Windows (QEMU + WHPX) | yes | yes | no |
| Mac, Apple silicon | planned (ARM) | planned (Windows 11 ARM) | yes, through terminal-mac |

macOS runs only on Apple hardware (Apple's licence; Apple Virtualization needs Apple silicon). On a Mac, terminal-vm hands macOS VMs to [terminal-mac](https://github.com/govijr/terminal-mac) with the arguments unchanged: `up` and `create` without `--os` (or with `--os macos`), and any command naming one of terminal-mac's VMs, `mcp NAME` included. `ls` lists both. Set `TERMINAL_MAC_BIN` if terminal-mac is not on the PATH.

For Linux and Windows guests, the host drives QEMU over QMP (only the optional guest agent runs inside): `screendump` captures the screen, `input-send-event` / `send-key` inject input through a virtual USB tablet and keyboard. The host desktop is never touched. One Python file, standard library only.

## Requirements

- Linux host with KVM (`/dev/kvm` accessible: `sudo usermod -aG kvm $USER`), QEMU >= 7.1, Python >= 3.8.
  Debian/Ubuntu: `sudo apt install qemu-system-x86 qemu-utils python3`
- Optional: `qemu-system-gui` (window at install time), `tigervnc-viewer` (`show`), a terminal with inline images for `view`: Kitty, Ghostty, WezTerm (Kitty graphics) or Windows Terminal >= 1.22, foot, mlterm (Sixel).
- A guest with USB tablet support (any current distribution).

## Install and run

```sh
sh install.sh            # ~/.local/bin/terminal-vm, no root; runs `doctor`
terminal-vm up        # downloads Debian live XFCE (SHA-256 verified), creates, boots, opens the viewer
```

`up --plan` previews the steps. `create NAME [--iso PATH] [--cpus 4 --memory 4 --disk 32 --width 1280 --height 800 --layout us|fr --offline]` customises a new VM.

## Use with agents

```sh
claude mcp add terminal-vm -- terminal-vm mcp default
terminal-vm skills install            # ~/.claude/skills/terminal-vm/SKILL.md
```

or install this folder as a Claude Code plugin (`.claude-plugin/`).

## Commands

| Command | |
|---|---|
| `ls`, `status NAME`, `doctor`, `help`, `version` | list VMs, state + screen + logged-in users, host checks |
| `start NAME [--window]`, `pause`, `resume` | lifecycle |
| `web [--project P] [--no-open]` | web dashboard of the VMs and, when installed, terminal-ct's containers (see [Web dashboard](#web-dashboard)) |
| `daemon run\|start\|stop\|status`, `supervise NAME [--restart always\|never] [--idle-pause MIN]`, `events [-n 50] [--vm V]` | supervision: restarts, health, idle pause, events (see [Supervision](#supervision)) |
| `resume [--stay]`, `autostart [on\|off]` | start again the VMs that were running before a restart; at each logon (see [Restarts](#restarts)) |
| `project ls`, `project show [P]`, `project notes [P [TEXT]]`, `project move NAME P`; `create --project P` | projects (see [Projects](#projects)) |
| `stop NAME [--timeout 60]` / `--force` | asks the guest and waits; reports a guest showing a confirmation dialog |
| `rm NAME` / `reset NAME` | delete a stopped VM / give it a blank disk of the same size (reinstalls on the next start, Windows answers kept) |
| `clone SRC NEW` | copy-on-write copy of a stopped VM in under a second: install once, clone many (Windows Server 2025 clone ready in 0.3 s + 28 s boot instead of a 37-min install). SRC's disk moves to a read-only base in `.bases/` of the store and SRC becomes an overlay on it too, so both keep running and changing independently; clones of an unchanged SRC share one base, and a base is deleted with its last VM (`rm`/`reset`). SRC must have no snapshots (they would stay in the base). Clones keep SRC's account. A Windows clone with the guest agent takes its own computer name (from the VM name) at its first start, without a reboot: about 30 s after the agent answers, its session logs off and back on automatically; NetBIOS is turned off in it (the old name would stay registered until a reboot), names resolve through DNS/LLMNR |
| `snapshot NAME save\|load\|rm TAG`, `snapshot NAME ls` | qcow2 snapshots: whole machine with memory while running, disk only while stopped |
| `display NAME --width W --height H` | screen size at the next start |
| `layout NAME [us\|fr]` | show or change the keyboard layout used by `type`/`key` (us = QWERTY, fr = AZERTY), even while running; `action ... --layout` overrides it for one call |
| `image [--os OS] [--plan]` | download the installation image only |
| `push NAME LOCAL GUEST` / `pull NAME GUEST LOCAL` | copy files through qemu-guest-agent (must be installed in the guest) |
| `view NAME [--read-only]` | interactive terminal viewer (keyboard, mouse, wheel): Kitty graphics, else Sixel (adaptive 255-colour palette, text kept legible when shrunk), else the VNC window; centred, follows resizes, redraws only what changed; Ctrl+] exits, Ctrl+O opens the window |
| `show NAME [--read-only]` / `hide NAME` | native window through VNC (unix socket); `--read-only` needs TigerVNC |
| `action NAME screenshot\|click\|move\|drag\|scroll\|type\|key\|wait ...` | one action, JSON reply |
| `mcp [NAME] [--project P]` | stdio MCP server (also exposes `wait`: until the screen stops changing; with `screenshot: true` an action returns the screen once it has changed, up to 1.5 s) |
| `skills get\|path\|install` | agent skill |

The viewer has its own QMP monitor, so agents keep working while someone watches.

Under WSL, the distribution can be stopped when no WSL terminal is open, which kills its VMs (a Windows install cut short leaves an unbootable disk: `reset NAME`, then `start NAME`). Keep one open, e.g. `wsl -d Debian -- sleep infinity`; `doctor` warns about it. The WSL disk grows inside the Windows drive while `df` in WSL shows its virtual size: keep ~20 GB free on the Windows drive per Windows guest, or the distribution fails with I/O errors. 

## Windows guests

`--os` picks a hardware profile that each Windows version drives with its built-in drivers, and an answer file that installs it unattended (local administrator with automatic logon, OOBE skipped). `create` prints the generated user and password.

| `--os` | Machine | Disk | Network | Display | USB | Key | Notes |
|---|---|---|---|---|---|---|---|
| `winxp` | i440fx | IDE | rtl8139 | Cirrus | UHCI | **required** | `winnt.sif` on a virtual floppy; 32-bit ISO; generic `qemu64` CPU |
| `win7` | q35 | AHCI | e1000 | std VGA | EHCI | optional | no xHCI driver in Windows 7 |
| `win8` | q35 | AHCI | e1000 | std VGA | xHCI | **required** | Setup refuses to continue without a key |
| `win10`, `win11` | q35 | AHCI | e1000e | std VGA | xHCI | default: Microsoft's public KMS client key for Pro (installs, does not activate) | Windows 11: TPM, Secure Boot and CPU checks bypassed with Setup's LabConfig keys |
| `win2016` … `win2025` | q35 | AHCI | e1000e | std VGA | xHCI | none (evaluation media) | image 2: Standard with Desktop Experience |

```sh
terminal-vm up srv --os win2025            # downloads Microsoft's evaluation ISO, installs unattended
terminal-vm create w11 --os win11 --iso Win11_24H2_French_x64.iso --ui-language fr-FR --layout fr
terminal-vm create xp --os winxp --iso xp-sp3.iso --product-key XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
```

Only Windows Server 2025 evaluation media is downloaded automatically (straight from Microsoft); other versions need your own ISO and licence. `--ui-language` must be the ISO's language (default `en-US`), `--layout` sets the keyboard. Characters outside the keyboard layout are typed with Windows' hex Unicode entry (Alt, numpad +, hex code), enabled by the answer file. Windows 10/11 and Server 2016+ guests get the VirtIO serial driver and qemu-guest-agent at first logon (Fedora's virtio-win guest tools 0.1.302, ~31 MB, downloaded once over HTTPS and SHA-256 pinned, served from the answers drive), so `status` reports the logged-in user and `push`/`pull` work as on Linux. Older Windows, or a host that cannot download them, works without the agent (users reported as unknown, no file copy). Until the first clean `stop`, an installing Windows VM boots with its vCPU ceiling when the quota has room, and the first logon ends by logging off and back on automatically (2 minutes) instead of rebooting (8 minutes of "Updates are underway" on Server 2025). The answer file also sets the host's time zone (read with `tzutil`, from WSL too; UTC on other Linux hosts) with a local-time clock, and keeps Server Manager from opening full screen at every logon on Windows Server. Under KVM, Windows guests get Hyper-V enlightenments: on Server 2025, boot to the guest agent went from 30 to 19 s and idle CPU from 240 % to 40 %.

## Windows hosts

The same file runs on Windows 10/11 and Windows Server hosts with Python 3.8+ and QEMU (`winget install SoftwareFreedomConservancy.QEMU`). Enable the Windows Hypervisor Platform for speed (as administrator: `Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform`, then restart); without it QEMU falls back to slow emulation. `doctor` checks both. QMP and the guest agent use unix sockets in the VM's folder under your profile, as on Linux, so other users cannot reach them (Windows 10 1803+ and QEMU >= 7.2; Python reaches them through Winsock). VNC listens on a random localhost port behind a password generated at each start, which `show` prints for the viewer's prompt. Snapshots of a running VM are refused by QEMU on Windows hosts (no dirty-memory tracking): stop the VM first, then `snapshot NAME save TAG` (disk only). `view` draws the guest inside Windows Terminal (Sixel, keyboard and mouse through VT input), centred and scaled to the window, redrawing only what changed. Windows Terminal reports the mouse by cell only; on a Windows host the viewer refines it with the host cursor position, so the two pointers line up (the guest needs an absolute pointer: Windows and current Linux desktops have one, TinyCore's Xvesa does not). For the sharpest image, shrink the font (Ctrl+-) until the window holds the guest screen at 1:1 (about 103x39 cells for 1024x768). Other Windows terminals (WezTerm included) pass no images through ConPTY: `view` opens a VNC window there (TigerVNC), or use `start NAME --window`. Use `terminal-vm.cmd` from this folder as the launcher.

## Networks

Small infrastructures without administrator rights: `net create lab`, then `net attach VM lab` on stopped VMs (each gets a NIC with a stable MAC and a suggested address in 10.77.N.0/24), `net ls`, `net detach VM lab`, `net rm lab`. A network is a small learning switch (Python, UDP on the loopback interface) that `start` launches when a VM on it starts and that exits a minute or two after the last one stops; each NIC is a QEMU `dgram` socket plugged into it, the same on Linux, macOS and Windows hosts (QEMU >= 7.2). VMs on it reach each other, never the LAN or the internet (a local process could send to or read from the switch port). There is no DHCP: set the suggested addresses in the guests, or run a DHCP server in one of them. Internet access is QEMU's NAT, per VM: `internet NAME on|off` (stopped VM); a sandboxed VM stays offline unless the user turns it on for it. Bridging VMs onto the real LAN needs a one-time administrator setup and is not included yet. Verified: two Tiny Core guests on an isolated network ping each other (0% loss) and cannot reach the internet (Linux host under WSL, 2-6 ms); on a Windows host, a guest's ARP and pings cross the switch both ways to a test peer, and broadcasts reach every member. Networks made before the switch: `net rm` and `net create` them again.

## Quota

One quota covers everything the host runs, so neither people nor agents can overload it. `quota` shows the limits and their use (also an MCP tool, read-only); only the user changes them: `quota --cpus N --memory GiB --disk GiB --vms N`. Defaults come from the host: cores - 1, 75% of RAM, half of the disk space VMs can have, 8 running VMs.

| Counted | When |
|---|---|
| vCPUs of running VMs | `start`, `resources --cpus` (more CPUs on a running VM) |
| memory **ceilings** of running VMs (what QEMU may use: a guest can refuse to give memory back through the balloon) | `start` |
| virtual disk sizes (what guests can fill) | `create`, `clone` |
| running VMs | `start` |

Every check runs under a host-wide lock, so two commands cannot both pass and then both start. A VM's default ceiling never exceeds the quota. VMs already above the disk quota still start; new disks wait for room. Under WSL, `df` reports the virtual disk of the distribution, not the Windows drive: the default counts the free space of `C:` too (where WSL keeps it unless moved); if the distribution lives on another drive, set `--disk`.

`top` (also an MCP tool) lists every running VM's real use of the host, QEMU's CPU % of one core and resident memory, read from `/proc` or the Windows process API, next to its allotment and ceiling, followed by the quota.

## Resources

Each VM has a ceiling of CPUs and memory: its template's (`--os`: 8 CPUs and 16 GiB, Windows XP 2 and 3), never above the host, or `create --max-cpus N --max-memory GiB`. Within it, `resources NAME --cpus N --memory GiB` (MCP tool `resources`, so agents size their own VM) changes what the VM uses, live where QEMU and the guest allow it:

| | Live | Otherwise |
|---|---|---|
| Memory | up and down, through a VirtIO balloon: the VM boots with its ceiling and gives back what is above its allocation. Linux guests, and Windows 10/11 and Server 2016+ with the guest tools | next start (Windows XP/7/8, or no balloon driver) |
| CPUs | added on Linux guests (`-smp N,maxcpus=MAX`); qemu-guest-agent brings them online, else the reply says how | removal, and Windows guests (Standard editions cannot add CPUs live): next start |

Verified: Server 2025 went 4 → 2 → 5 GiB without restarting (QEMU's balloon report followed); Debian 13 went from 4 to 6 CPUs live (online once brought up).

## Sandbox

`create NAME --sandbox` (or `sandbox NAME on` for a stopped VM) isolates a VM for an untrusted agent. Only the screen and input reach the guest:

| | Sandbox |
|---|---|
| Network | none (`-nic none`) |
| Guest agent, `push`/`pull` | no agent channel; file copy refused |
| Disk | writes discarded at every stop (`snapshot=on`): each run starts from the same state. Prepare that state with sandbox off, or `snapshot NAME load TAG` while stopped. A Windows install still writes for real until it is finished |
| QEMU | seccomp on Linux hosts (`-sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny`); QEMU for Windows has none |
| MCP | screen and input tools only (as always); every call, failures included, is appended to `actions.log` in the VM folder |
| Snapshots | refused while running (they would be discarded) |

Not covered: running QEMU under a dedicated system user (needs administrator setup) and host-side CPU or I/O quotas. Verified on Windows Server 2025 in WSL: no network adapter in the guest, a folder created during a run gone after restart, actions logged.

## Agent authority

Through MCP an agent can build a small infrastructure on its own, within the quota: `vm_list`, `vm_create`, `vm_clone`, `vm_start`, `vm_stop`, `vm_remove`, `vm_snapshot`, `vm_resources`, `vm_internet`, `net_list`, `net_create`, `net_remove`, `net_attach`, `net_detach`. The screen tools take an optional `vm` to drive another VM it manages. It manages **sandboxed VMs and the VMs it created** (`created_by: agent` in `machine.json`, set by `vm_create`/`vm_clone`; only the CLI changes it). The user's other VMs and networks stay out of reach.

| Needs the user's approval | CLI the agent is told to ask for otherwise |
|---|---|
| internet for a VM | `internet NAME on` |
| raising a ceiling, or `vm_create` with `max_memory_gb` above the default | `resources NAME --max-cpus N --max-memory GiB` |
| downloading an installation image | `image --os OS` |
| plugging a VM into a network the user created | `net attach VM NET` |
| `vm_clone` of one of the user's VMs (the clone starts offline) | `clone SRC NEW` |
| `vm_remove`, `snapshot load\|rm` on a sandboxed VM the user created | `rm NAME`, `snapshot NAME load\|rm TAG` |

The server asks through MCP elicitation when the client supports it (a yes/no form); otherwise, or on refusal, the tool fails and names the command. `vm_create` takes only images already in `images/` of the store (never a host path), creates a sandbox by default, and leaves a non-sandboxed VM offline. Every infrastructure call, failures included, is appended to the project's `agent.log` (`STORE/.projects/P/`). The `exec` tool runs a shell command in the guest through qemu-guest-agent (Linux: `sh -c` as root; Windows: PowerShell as SYSTEM; not in sandboxes, which have no guest agent): on the server's own VM, or on another VM the agent manages. Verified under WSL: an agent with MCP alone created two sandboxed Tiny Core VMs and a network, configured them, pinged one from the other (0% loss), then removed everything.

## Restarts

- `start` and `stop` record what each VM should be doing (`want` in its folder). After the PC (or WSL) restarts,
  `resume` (no VM name) starts again the VMs that were running, within the quota, and leaves alone those stopped on
  purpose. A VM shut down from inside the guest is recorded as stopped (its watcher sees the guest's own
  shutdown); one whose QEMU was killed, or the PC cut, comes back.
- `autostart on` runs it at each logon: on Windows (also from WSL) a scheduled task `terminal-vm-resume` (from
  WSL: `conhost --headless wsl.exe ... resume --stay`, which keeps WSL up while VMs run); on Linux a systemd user
  service (at login; `loginctl enable-linger` for boot without login). `autostart off` removes it, `autostart` alone
  tells whether it is on. To turn it on:

  ```sh
  # from Windows, terminal-vm in WSL (Claude Code: prefix with ! to run it in the session)
  wsl -d Debian -- python3 /path/to/terminal_vm.py autostart on
  # Windows host, or Linux, or inside WSL
  python3 terminal_vm.py autostart on
  ```
- Containers inside a VM: install [terminal-ct](https://github.com/superkaliiii/terminal-conteneur) in the guest and
  run `terminal-ct autostart on` there (as the guest's user); with the VM itself on autostart, its containers come
  back too. Its containers can sit under the VM's project: `TERMINAL_CT_PROJECT=lab/<vm>` in the guest.
- Agents: the MCP tool `autostart` shows the setting and turns it on or off with the user's approval; with `vm`,
  it does that for terminal-ct inside that Linux VM (run as the logged-in user through the guest agent), also with
  approval.

## Supervision

`start` also starts the supervision daemon (`terminal-vm daemon run|start|stop|status`; one per store, it ends by
itself 10 minutes after the last VM that should run). Every 5 s it:

- starts again a VM that should run but stopped without a guest shutdown (QEMU crashed or was killed: checked on
  the disk's lock, or on Windows QEMU's process, since QMP answers one client at a time), after 5 s,
  then 10, 20... up to 5 min between tries while it keeps failing (counted again from 5 s once it stayed up 10 min);
  a refusal (quota) is retried the same way. `supervise NAME --restart never` leaves such a VM down; closing the
  window of a VM started with `--window` stops it for good. The daemon and `start`, `stop`, `rm`, `reset`,
  `clone` and `snapshot` take a per-VM lock, so a restart never runs across one of them.
- pings the guest agent of running VMs: `unhealthy` after 3 misses in a row (not in the first 5 minutes, not in
  the minute after a use, only for VMs whose agent answered once), `healthy` when it answers again. Reported, not
  acted on: a VM holds state.
- pauses a VM nobody used for its `supervise NAME --idle-pause MINUTES` (no screen action, `exec`, file copy,
  open `view`, or VNC client such as `show`) that is not computing either (QEMU under 20 % of a core; an idle guest
  costs it 4-12 %). The next use wakes it at once (`stop` too); a pause by the user (`pause`) stays. Off by
  default (`--idle-pause 0`). A guest paused for long may find its clock behind when it wakes (Windows syncs it
  later; Linux with an NTP client).

What it does goes to `STORE/events.log` (`events [-n 50] [--vm V]`: `restarted`, `restart_failed`, `unhealthy`,
`healthy`, `idle_paused`, `woken`, `window_closed`, `supervise_failed`, `daemon_started`/`stopped`), in
terminal-ct's line format (`vm` where it has `container`). Linux guests also
give unused memory back to the host while they run (virtio-balloon with free page reporting). Measured (WSL,
TinyCore): QEMU killed -> running again in 3 s; idle pause and wake by a screenshot as described.
`TERMINAL_VM_DAEMON=0` turns supervision off.

## Web dashboard

`terminal-vm web [--project P] [--no-open]` starts the daemon if needed and opens its dashboard in the browser
(from WSL, the Windows one): VMs (state, project, CPU and memory, start, stop, force stop, screen thumbnails and a
larger refreshed view), the quota, the events, and [terminal-ct](https://github.com/superkaliiii/terminal-conteneur)'s
containers and stacks when it is installed (on the PATH, or `TERMINAL_CT="python3 /path/terminal_ct.py"`): its
`dash --json`, logs, start, stop, restart, remove, scale, the stack editor and its screenshots, all through its
command line. It is the same dashboard as `terminal-ct web` (one block of code, identical in both tools).

- Served by the supervision daemon on 127.0.0.1 only, port `TERMINAL_VM_WEB_PORT` (default 7172; terminal-ct uses
  7171, so both run side by side). The daemon stays up while a dashboard is open.
- Every API call needs the token of `STORE/web.token` (owner only, kept across restarts), passed by `web` in the URL
  fragment; other Host names are refused.
- Thumbnails are not a use: watching a VM does not keep it from its idle pause; its larger view neither. Use `view`
  or `show` to work with it.

## Projects

- A project groups VMs and networks (`create --project P`, `net create NET --project P`; otherwise
  `TERMINAL_VM_PROJECT`, else `default`) with their notes and agent log (`STORE/.projects/P/`). Sub-projects are
  paths (`lab/web`, up to 4 levels); whoever has `lab` also has `lab/web`. `project move NAME P` only relabels.
- `mcp --project P`: the agent sees and reaches only P's VMs and networks (and its sub-projects'), creates and clones
  into P or below; outside P nothing is listed and nothing can be approved, so each agent keeps to its own project.
  Its `quota` and `top` show the room left and its project's use, not the other projects'; a name taken elsewhere
  is just "not available".
- To resume, an agent calls `project_status` (VMs and networks with what each should be doing, notes, latest
  actions with errors); `notes_write` keeps notes for the next session. The server says so at connection
  (MCP `instructions`).

## Keyboard layouts and text

`type` sends key presses in the guest's layout (`--layout us|fr` at creation, stored per VM, changed with `layout NAME us|fr`; the MCP `type` and `key` tools also take an optional `layout`). It must match the layout active in the guest. Characters outside the layout use Ctrl+Shift+U Unicode entry (GTK/Qt apps). Key chords name letters as the guest layout prints them (`ctrl+a` on an AZERTY guest presses the physical Q key).

## Login detection

With `qemu-guest-agent` installed in the guest, `status` reports the active graphical session's user by reading systemd-logind's state files through the agent (read-only, nothing executed). The MCP server refuses input actions only when the guest is definitely at a login screen; without the agent, the state is reported as unknown.

## Tests

```sh
python3 -m unittest -v      # layouts, viewer input parsing, QMP and guest-agent protocol against fake servers
```
