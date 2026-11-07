# terminal-linux

A separate Linux or Windows desktop (QEMU, on Linux or Windows hosts) for computer-use agents, viewed and controlled from a terminal: the Linux and Windows port of terminal-mac, with the same MCP tools (`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `status`).

Nothing is installed in the guest. The host drives QEMU over QMP: `screendump` captures the screen, `input-send-event` / `send-key` inject input through a virtual USB tablet and keyboard. The host desktop is never touched. One Python file, standard library only.

## Requirements

- Linux host with KVM (`/dev/kvm` accessible: `sudo usermod -aG kvm $USER`), QEMU >= 7.1, Python >= 3.8.
  Debian/Ubuntu: `sudo apt install qemu-system-x86 qemu-utils python3`
- Optional: `qemu-system-gui` (window at install time), `tigervnc-viewer` (`show`), a terminal with inline images for `view`: Kitty, Ghostty, WezTerm (Kitty graphics) or Windows Terminal >= 1.22, foot, mlterm (Sixel).
- A guest with USB tablet support (any current distribution).

## Install and run

```sh
sh install.sh            # ~/.local/bin/terminal-linux, no root; runs `doctor`
terminal-linux up        # downloads Debian live XFCE (SHA-256 verified), creates, boots, opens the viewer
```

`up --plan` previews the steps. `create NAME [--iso PATH] [--cpus 4 --memory 4 --disk 32 --width 1280 --height 800 --layout us|fr --offline]` customises a new VM.

## Use with agents

```sh
claude mcp add terminal-linux -- terminal-linux mcp default
terminal-linux skills install            # ~/.claude/skills/terminal-linux/SKILL.md
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
| `view NAME [--read-only]` | interactive terminal viewer (keyboard, mouse, wheel): Kitty graphics, else Sixel (216 colours), else the VNC window; Ctrl+] exits, Ctrl+O opens the window |
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
terminal-linux up srv --os win2025            # downloads Microsoft's evaluation ISO, installs unattended
terminal-linux create w11 --os win11 --iso Win11_24H2_French_x64.iso --ui-language fr-FR --layout fr
terminal-linux create xp --os winxp --iso xp-sp3.iso --product-key XXXXX-XXXXX-XXXXX-XXXXX-XXXXX
```

Only Windows Server 2025 evaluation media is downloaded automatically (straight from Microsoft); other versions need your own ISO and licence. `--ui-language` must be the ISO's language (default `en-US`), `--layout` sets the keyboard. Characters outside the keyboard layout are typed with Windows' hex Unicode entry (Alt, numpad +, hex code), enabled by the answer file. Windows guests have no guest agent by default, so `status` reports users as unknown.

## Windows hosts

The same file runs on Windows 10/11 and Windows Server hosts with Python 3.8+ and QEMU (`winget install SoftwareFreedomConservancy.QEMU`). Enable the Windows Hypervisor Platform for speed (as administrator: `Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform`, then restart); without it QEMU falls back to slow emulation. `doctor` checks both. QMP and the guest agent use unix sockets in the VM's folder under your profile, as on Linux, so other users cannot reach them (Windows 10 1803+ and QEMU >= 7.2; Python reaches them through Winsock). VNC listens on a random localhost port behind a password generated at each start, which `show` prints for the viewer's prompt. Snapshots of a running VM are refused by QEMU on Windows hosts (no dirty-memory tracking): stop the VM first, then `snapshot NAME save TAG` (disk only). `view` draws the guest inside Windows Terminal (Sixel, keyboard and mouse through VT input), centred and scaled to the window, redrawing only what changed. Windows Terminal reports the mouse by cell, so the guest pointer moves on a grid of about 10x20 image pixels. Other Windows terminals (WezTerm included) pass no images through ConPTY: `view` opens a VNC window there (TigerVNC), or use `start NAME --window`. Use `terminal-linux.cmd` from this folder as the launcher.

## Keyboard layouts and text

`type` sends key presses in the guest's layout (`--layout us|fr` at creation, stored per VM, changed with `layout NAME us|fr`; the MCP `type` and `key` tools also take an optional `layout`). It must match the layout active in the guest. Characters outside the layout use Ctrl+Shift+U Unicode entry (GTK/Qt apps). Key chords name letters as the guest layout prints them (`ctrl+a` on an AZERTY guest presses the physical Q key).

## Login detection

With `qemu-guest-agent` installed in the guest, `status` reports the active graphical session's user by reading systemd-logind's state files through the agent (read-only, nothing executed). The MCP server refuses input actions only when the guest is definitely at a login screen; without the agent, the state is reported as unknown.

## Tests

```sh
python3 -m unittest -v      # layouts, viewer input parsing, QMP and guest-agent protocol against fake servers
```
