# Contributing

Thanks for helping. This repository holds terminal-mac (Swift, macOS guests on Apple silicon) and terminal-vm
(Python, QEMU guests on Linux, Windows and macOS hosts). Both stay small on purpose; these rules keep them that way.

## Ground rules

- **terminal-vm is one file, standard library only.** `terminal-vm/terminal_vm.py` runs on Python 3.9+ with
  nothing to install besides QEMU. A change that needs a Python dependency will not be merged.
- **terminal-mac has no dependency outside Swift and Apple's frameworks.**
- **Same shape everywhere.** Commands, MCP tools and JSON replies keep the names and arguments the other tools
  (terminal-mac, terminal-vm, terminal-ct) use for the same thing.
- **Errors say what to do.** A failure names the cause and the command that fixes it, as `doctor` does.
- **Nothing personal in the repository**: no absolute paths, user or host names, tokens.

## Before a pull request

```sh
cd terminal-vm && python3 -m unittest -v   # terminal-vm, must pass on every OS
swift test                                 # terminal-mac, on a Mac with Xcode 16 / Swift 6
```

- New behavior comes with a test; a bug fix with the test that failed before it.
- Update the README of the tool (command table and the matching section) and its agent skill under `skills/` when a
  command or MCP tool changes.
- One change per pull request, with a message that says what changes for the user and why.

## Reporting

Bugs: an issue with the tool's version, the host OS, the steps, and `doctor` output if it helps. Security
problems: see [SECURITY.md](SECURITY.md), never a public issue.

By contributing you agree that your work is released under the [MIT license](LICENSE).
