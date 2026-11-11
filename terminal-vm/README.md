# terminal-vm

A separate desktop VM for computer-use agents, viewed and driven from a terminal.

One command and one MCP server for Linux, Windows and macOS guests: `screenshot`, `click`, `move`, `drag`, `scroll`,
`type`, `key`, `status`, and the host desktop is never touched. One Python file, standard library only; QEMU on
Linux and Windows hosts, terminal-mac for macOS guests on Apple silicon.

```sh
terminal-vm doctor        # what is missing on this host
terminal-vm up            # a Debian desktop VM, unattended
terminal-vm view NAME     # watch it in the terminal
terminal-vm mcp NAME      # MCP server for an agent
```

Full documentation: https://github.com/superkaliiii/terminal-vm

MIT licence.
