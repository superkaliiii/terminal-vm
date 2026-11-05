# terminal-linux

A separate Linux desktop (QEMU/KVM) for computer-use agents, viewed and controlled from a terminal: the Linux port of terminal-mac, with the same MCP tools (`screenshot`, `click`, `move`, `drag`, `scroll`, `type`, `key`, `status`).

Nothing is installed in the guest. The host drives QEMU over QMP: `screendump` captures the screen, `input-send-event` / `send-key` inject input through a virtual USB tablet and keyboard. The host desktop is never touched. One Python file, standard library only.

## Requirements

- Linux host with KVM (`/dev/kvm` accessible: `sudo usermod -aG kvm $USER`), QEMU >= 7.1, Python >= 3.8.
  Debian/Ubuntu: `sudo apt install qemu-system-x86 qemu-utils python3`
- Optional: `qemu-system-gui` (window at install time), `tigervnc-viewer` (`show`), Kitty / Ghostty / WezTerm (inline `view`).
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
| `ls`, `status NAME`, `doctor` | list VMs, state + screen + logged-in users, host checks |
| `start NAME [--window]`, `pause`, `resume` | lifecycle |
| `stop NAME [--timeout 60]` / `--force` | asks the guest and waits; reports a guest showing a confirmation dialog |
| `view NAME` | interactive terminal viewer (keyboard, mouse, wheel); Ctrl+] exits, Ctrl+O opens the window |
| `show NAME` / `hide NAME` | native window through VNC (unix socket) |
| `action NAME screenshot\|click\|move\|drag\|scroll\|type\|key ...` | one action, JSON reply |
| `mcp [NAME]` | stdio MCP server |

The viewer has its own QMP monitor, so agents keep working while someone watches.

## Keyboard layouts and text

`type` sends key presses in the guest's layout (`--layout us|fr`, stored per VM). Characters outside the layout use Ctrl+Shift+U Unicode entry (GTK/Qt apps). Key chords name letters as the guest layout prints them (`ctrl+a` on an AZERTY guest presses the physical Q key).

## Login detection

With `qemu-guest-agent` installed in the guest, `status` reports the active graphical session's user by reading systemd-logind's state files through the agent (read-only, nothing executed). The MCP server refuses input actions only when the guest is definitely at a login screen; without the agent, the state is reported as unknown.

## Tests

```sh
python3 -m unittest -v      # layouts, viewer input parsing, QMP and guest-agent protocol against fake servers
```
