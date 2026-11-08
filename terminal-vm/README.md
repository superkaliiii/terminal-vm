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
| `stop NAME [--timeout 60]` / `--force` | asks the guest and waits; reports a guest showing a confirmation dialog |
| `rm NAME` / `reset NAME` | delete a stopped VM / give it a blank disk of the same size (reinstalls on the next start, Windows answers kept) |
| `snapshot NAME save\|load\|rm TAG`, `snapshot NAME ls` | qcow2 snapshots: whole machine with memory while running, disk only while stopped |
| `display NAME --width W --height H` | screen size at the next start |
| `layout NAME [us\|fr]` | show or change the keyboard layout used by `type`/`key` (us = QWERTY, fr = AZERTY), even while running; `action ... --layout` overrides it for one call |
| `image [--os OS] [--plan]` | download the installation image only |
| `push NAME LOCAL GUEST` / `pull NAME GUEST LOCAL` | copy files through qemu-guest-agent (must be installed in the guest) |
| `view NAME [--read-only]` | interactive terminal viewer (keyboard, mouse, wheel): Kitty graphics, else Sixel (adaptive 255-colour palette, text kept legible when shrunk), else the VNC window; centred, follows resizes, redraws only what changed; Ctrl+] exits, Ctrl+O opens the window |
| `show NAME [--read-only]` / `hide NAME` | native window through VNC (unix socket); `--read-only` needs TigerVNC |
| `action NAME screenshot\|click\|move\|drag\|scroll\|type\|key\|wait ...` | one action, JSON reply |
| `mcp [NAME]` | stdio MCP server (also exposes `wait`: until the screen stops changing) |
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

Only Windows Server 2025 evaluation media is downloaded automatically (straight from Microsoft); other versions need your own ISO and licence. `--ui-language` must be the ISO's language (default `en-US`), `--layout` sets the keyboard. Characters outside the keyboard layout are typed with Windows' hex Unicode entry (Alt, numpad +, hex code), enabled by the answer file. Windows 10/11 and Server 2016+ guests get the VirtIO serial driver and qemu-guest-agent at first logon (Fedora's virtio-win guest tools 0.1.302, ~31 MB, downloaded once over HTTPS and SHA-256 pinned, served from the answers drive), so `status` reports the logged-in user and `push`/`pull` work as on Linux. Older Windows, or a host that cannot download them, works without the agent (users reported as unknown, no file copy). Until the first clean `stop`, an installing Windows VM boots with its vCPU ceiling when the quota has room, and the first logon ends by logging off and back on automatically (2 minutes) instead of rebooting (8 minutes of "Updates are underway" on Server 2025). Under KVM, Windows guests get Hyper-V enlightenments: on Server 2025, boot to the guest agent went from 30 to 19 s and idle CPU from 240 % to 40 %.

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
| `vm_remove`, `snapshot load\|rm` on a sandboxed VM the user created | `rm NAME`, `snapshot NAME load\|rm TAG` |

The server asks through MCP elicitation when the client supports it (a yes/no form); otherwise, or on refusal, the tool fails and names the command. `vm_create` takes only images already in `images/` of the store (never a host path), creates a sandbox by default, and leaves a non-sandboxed VM offline. Every infrastructure call, failures included, is appended to `agent.log` in the store. Verified under WSL: an agent with MCP alone created two sandboxed Tiny Core VMs and a network, configured them, pinged one from the other (0% loss), then removed everything.

## Keyboard layouts and text

`type` sends key presses in the guest's layout (`--layout us|fr` at creation, stored per VM, changed with `layout NAME us|fr`; the MCP `type` and `key` tools also take an optional `layout`). It must match the layout active in the guest. Characters outside the layout use Ctrl+Shift+U Unicode entry (GTK/Qt apps). Key chords name letters as the guest layout prints them (`ctrl+a` on an AZERTY guest presses the physical Q key).

## Login detection

With `qemu-guest-agent` installed in the guest, `status` reports the active graphical session's user by reading systemd-logind's state files through the agent (read-only, nothing executed). The MCP server refuses input actions only when the guest is definitely at a login screen; without the agent, the state is reported as unknown.

## Tests

```sh
python3 -m unittest -v      # layouts, viewer input parsing, QMP and guest-agent protocol against fake servers
```
