# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository
([private vulnerability reporting](https://github.com/superkaliiii/terminal-vm/security/advisories/new)).
Do not open a public issue for a security problem.

Include the tool and its version (`terminal-vm version`, `terminal-mac --version`), the host OS, the steps that
show the problem, and what an attacker gains. `terminal-vm doctor` output helps; remove anything private from it.

You should get a first answer within 7 days. A fix is released as soon as it is ready, and the advisory is
published with it, crediting you unless you prefer otherwise.

## Supported versions

Only the latest release receives security fixes.

## Scope

In scope: what these tools themselves enforce, for example

- terminal-vm sandboxes (no network, disk changes discarded) and the authority rules that decide what an agent may
  run through MCP without asking;
- the web dashboard, its users and roles, the agent and relay between PCs (TLS, fingerprints, tokens);
- the guest helpers of terminal-mac and terminal-vm, and what they accept from the host;
- files the tools write (tokens, keys, users) and their permissions;
- the installers and the integrity checks of what they download.

Out of scope: escapes through QEMU, Apple Virtualization or the host OS themselves (report them upstream), and
anything that needs an account already allowed to run these tools on that PC.
