---
name: terminal-linux
description: Operate a separate virtual Linux desktop (QEMU/KVM) through the terminal-linux MCP tools or CLI - observe with screenshots, act with click/type/key, verify. Use when a task needs a GUI Linux desktop that is not the user's own machine.
---

# Using terminal-linux

terminal-linux gives you computer use inside a separate virtual Linux machine. Your actions never touch the user's own desktop, files, clipboard or sessions.

## Tools (MCP server `terminal-linux mcp NAME`)

`screenshot`, `click` (`clicks: 2|3`, `button: right|middle`), `move`, `drag`, `scroll` (`dy`, negative scrolls down, 100 per notch), `type`, `key`, `status`. Coordinates are pixels of the latest `screenshot`. Action tools accept `screenshot: true` to return a fresh screenshot with the result.

## Loop: observe -> act -> verify

1. `status`. If it says the VM is not running, ask the user to run `terminal-linux up NAME` (or `start NAME`). If paused, `terminal-linux resume NAME`.
2. `screenshot` and read it before every decision. Never guess coordinates.
3. Do one meaningful action, then take a new screenshot to confirm the result.
4. Prefer keyboard shortcuts when they are reliable (`ctrl+l` in a browser, `alt+f2` run dialog, `ctrl+alt+t` terminal on many desktops).

## Text and keys

- `type` sends text in the guest's keyboard layout (set per VM with `--layout us|fr`). Characters outside that layout go through Ctrl+Shift+U Unicode entry, which works in GTK/Qt apps but not in raw consoles.
- `key` takes chords such as `ctrl+a`, `alt+tab`, `ctrl+alt+t`, `return`, `escape`, `super`, `f5`. Letters name the letter in the guest layout.

## When to ask the user

- A tool reports no logged-in session (login screen), a password, or credentials are needed.
- The guest asks a confirmation you are not sure about (shutdown, install, delete).

## Lifecycle (CLI)

`terminal-linux ls`, `status NAME`, `pause NAME`, `resume NAME`, `stop NAME` (asks the guest; it may show a confirmation dialog you can click), `stop NAME --force` (power cut, can lose work: only when the user agrees). Do not delete VM data.
