---
name: terminal-vm
description: Operate a separate virtual desktop (Linux or Windows in QEMU; macOS on a Mac through terminal-mac) with the terminal-vm MCP tools or CLI - observe with screenshots, act with click/type/key, verify. Use when a task needs a GUI desktop that is not the user's own machine.
---

# Using terminal-vm

terminal-vm gives you computer use inside a separate virtual machine (Linux, Windows, or macOS on a Mac host). Your actions never touch the user's own desktop, files, clipboard or sessions.

## Tools (MCP server `terminal-vm mcp NAME`)

`screenshot`, `click` (`clicks: 2|3`, `button: right|middle`), `move`, `drag`, `scroll` (`dy`, negative scrolls down, 100 per notch), `type`, `key`, `status`, `wait` (until the screen stops changing, then a screenshot; `stable` seconds, `timeout`; a blinking cursor can keep it from settling, so read `stable` in the reply). Coordinates are pixels of the latest `screenshot`. Action tools accept `screenshot: true` to return a screenshot with the result, taken once the screen has changed (up to 1.5 s: a busy guest shows keystrokes late); if the effect is still missing, `wait` then `screenshot`.

## Start and end of a session

First call `infra`: the map in one call (this PC's quota with what is free, every VM with its state, ceiling
and use, networks and addresses, description, creator and `managed`, each network with its members, the projects,
and terminal-ct's containers, nodes and other PCs when it is installed). Give what you create a one-line
`description` (`vm_create`, `vm_clone`, `net_create`) or set it later with `describe`; describing the user's VMs
asks them. Then `project_status`: the VMs and networks of your project (with `want`, what each should be doing), the notes earlier sessions left, and their latest actions and errors. Pick up from there instead of rebuilding. Before you stop, `notes_write` what you set up, what works and what is left. A server started with `--project` shows you only that project (and its sub-projects, `lab/web`): the rest is not yours. `exec` runs a shell command in a managed, non-sandboxed VM (guest agent); for anything that may outlast its timeout (builds, test suites, a server), `background: true` returns a job id at once and `job` gives its exit code or `running` and the end of its output (`kill: true` stops it). A VM of another PC is `vm: "VM@PC"` (the PCs `infra` lists): screen tools, `exec`, `vm_start`, `vm_stop`; the user's VMs there ask them first; create, clone and remove there with `terminal-ct --host PC vm ...`. `autostart` (with the user's approval) brings running VMs back after the PC restarts; with `vm`, it turns terminal-ct's autostart on inside that Linux VM.

## Loop: observe -> act -> verify

1. `status`. If it says the VM is not running, ask the user to run `terminal-vm up NAME` (or `start NAME`). If paused, `terminal-vm resume NAME`. A VM the supervisor paused because nobody used it wakes by itself at your first action.
2. `screenshot` and read it before every decision. Never guess coordinates.
3. Do one meaningful action, then take a new screenshot to confirm the result.
4. Prefer keyboard shortcuts when they are reliable (`ctrl+l` in a browser, `alt+f2` run dialog, `ctrl+alt+t` terminal on many desktops).

## Text and keys

- `type` sends text in the guest's keyboard layout (set per VM with `--layout us|fr`, changed with `terminal-vm layout NAME us|fr`). `type` and `key` accept `layout: us|fr` for one call, e.g. when the guest's active keyboard differs (wrong letters such as `q` for `a` are the symptom). Characters outside that layout go through Ctrl+Shift+U Unicode entry, which works in GTK/Qt apps but not in raw consoles.
- `key` takes chords such as `ctrl+a`, `alt+tab`, `ctrl+alt+t`, `return`, `escape`, `super`, `f5`. Letters name the letter in the guest layout.

## Windows guests

`terminal-vm ls` shows each VM's `os`. Windows guests log on automatically after their unattended install; use Windows shortcuts (`super` opens Start, `super+r` Run, `ctrl+shift+escape` Task Manager). `status` reports users as unknown there: rely on the screenshot.

## When to ask the user

- A tool reports no logged-in session (login screen), a password, or credentials are needed.
- The guest asks a confirmation you are not sure about (shutdown, install, delete).

## Lifecycle (CLI)

`terminal-vm ls`, `status NAME`, `pause NAME`, `resume NAME`, `stop NAME` (asks the guest; it may show a confirmation dialog you can click; a locked Windows session ignores it: unlock with `key ctrl+alt+delete`, the password, `key enter`), `stop NAME --force` (power cut, can lose work: only when the user agrees). Do not delete VM data.

Before a risky step, `terminal-vm snapshot NAME save TAG` (running VM: memory included); `snapshot NAME load TAG` returns to it, `snapshot NAME ls` lists them. Files: `terminal-vm push NAME LOCAL GUEST` / `pull NAME GUEST LOCAL` (needs qemu-guest-agent in the guest). `rm` and `reset` destroy data: only when the user asks.

A sandboxed VM (`ls` shows `"sandbox": true`) has no network and forgets its disk changes when it stops; your actions are logged. Do not count on downloads, file copies or saved work there.

If the VM is too slow or short of memory, `resources` shows its CPUs and memory and its ceiling; ask for what the task needs within the ceiling, and give it back when done.

## Building VMs and networks yourself

`vm_list` marks with `managed: true` the VMs you may run: sandboxed VMs and the ones you created. Never try to run the user's other VMs. `vm_create` (sandboxed by default; `iso` names an image in the store) then `vm_start`; `vm_clone` a prepared VM instead of reinstalling (seconds instead of a long install; both stay usable; cloning one of the user's VMs needs their approval); when the user filled a warm pool (`infra` or `vm_list` show its paused members), `vm_take` with the pool's VM gives you a running one at once, no boot; `net_create` + `net_attach` (stopped VMs) for a private network, then set the suggested static address in each guest (no DHCP). The screen tools take `vm` to act on another managed VM. Stay within `quota`: set `max_memory_gb` = `memory_gb` to fit more small VMs. Internet, a higher ceiling, an image download or the user's network need the user's approval: the client may ask them, otherwise the error names the command to ask them to run. Remove what you created when the task is done (`vm_stop`, `vm_remove`, `net_remove`).
