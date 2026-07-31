# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Ansible-managed homelab infrastructure. Docker Swarm cluster (rackvm1-3 managers + rackvm4-5 workers, amd64 VMs on the pve1-5 Proxmox cluster) running user-facing services (sourcebot, youtrack, home assistant, arr stack, etc.). A Mac mini (`rackmini1`) runs native-only services outside the swarm (Plex, Homebridge, Tailscale, SMB mounts). Single main playbook (`playbooks/default.yml`) with tag-based execution.

## macOS hosts — required manual steps at bootstrap

macOS has a small number of operations that cannot be scripted without GUI interaction, because sshd's sandbox profile and TCC (Transparency, Consent & Control) grants are settable only through the GUI. The playbook DETECTS when these are missing and halts with an interactive `pause:` prompt + 10-minute timeout and explicit recovery instructions — rather than silently half-working. Expected manual steps at first-time bootstrap of a Mac host:

- **Register autofs SMB map in `/etc/auto_master`** — autofs only reads the `generic/smb-mount` map (`/etc/auto_smb`) when `/etc/auto_master` carries a `/-  auto_smb` direct-map line. The role writes that line directly with `lineinfile` (it runs as root over SSH, which *can* write `/etc/auto_master` — verified on-host). macOS OS updates reset `/etc/auto_master` to the stock default and strip the line, so **re-run the role after an OS update** (`make run hosts=<host> tags=smb-mount`) to restore it — already part of bringing a Mac back after an update. (An earlier version used a `local.smb-mount-fixup` boot LaunchDaemon to do this write, on the mistaken belief that the SSH/become path was sandbox-blocked; the daemon actually failed with EPERM from its own lack of Full Disk Access, and has been removed.) No manual step on either present or absent side.
- **Plex "Open at Login"** — the Plex menu-bar app registers its own LaunchAgent via LSSharedFileList, which can't be flipped without Automation TCC (which in turn can't be granted programmatically). After `generic/plex` installs the cask, the operator must launch Plex once via VNC/console and tick "Open at Login" in the menu bar. The role prints instructions if the LaunchAgent isn't registered yet.
- **FileVault login on reboot** — after a power cycle the Mac sits at the login screen until a human logs in; user LaunchAgents only run after login. No workaround; design-accepted.

## Common Commands

```bash
make after-git-pull     # Install all deps after pulling
make run                # Run default playbook against all hosts
make run hosts=rackvm1  # Limit to specific host
make run tags=docker    # Run specific tag (auto-skips base,common)
make dry-run            # Check mode
make check              # Syntax validation

# Any other playbook by name: playbooks/<name>.yml, with playbooks/hosts-<name>
# as its inventory when that file exists. hosts=/tags= work here too.
make bootstrap-proxmox user=root   # First contact: asks for the PAM password,
                                  # authorises the repo key for root, then forms
                                  # or joins the cluster
make run-proxmox        # Run playbooks/proxmox.yml against hosts-proxmox
make run-proxmox hosts=pve3 tags=proxmox-cluster
make dry-run-proxmox
make check-proxmox

# Vault
make vault-diff         # Show vault file changes (use for commit messages)
make vault-encrypt      # Encrypt all vault files (required before commit)
make vault-decrypt      # Decrypt all vault files for editing
make vault-check        # Verify encryption (runs as pre-commit hook)

# Dependencies
make -B requirements.txt              # Recompile Python deps
make -B requirements.txt UPGRADE=1    # Upgrade unpinned packages
make galaxy-install                   # Reinstall Ansible Galaxy roles/collections

# Bootstrap new host
make bootstrap hosts=<host> user=<user>
```

## Architecture

**Playbook**: `playbooks/default.yml` is a monolithic ~7400-line playbook organized as sequential plays controlled by tags: `always`, `base`, `common`, `docker`, `tailscale`, `telegraf`, etc.

**Inventory**: `playbooks/hosts` — hosts are grouped by function (docker, docker_swarm_manager, docker_swarm_worker, raspberry-pi, tailscale, cifs-shares, etc.) with inline variables.

**Host config**: `playbooks/host_vars/{hostname}/` contains `vars.yml` (plain) and `vault.yml` (encrypted). The vault files contain the full Docker stack/compose definitions inline as YAML (`vault_docker_stack_definition`, `docker_compose_definition`).

**Group config**: `playbooks/group_vars/{group}/vars.yml` for group-wide settings.

**Roles**: `playbooks/roles/` — `ansible/` (connection/bootstrap), `common/` (base config), `generic/` (18 custom roles like docker, telegraf, project-files, raspberry-pi), `galaxy/` (downloaded).

**Config file deployment**: The `project-files` role copies from `playbooks/files/plaintext/{hostname}/` and `playbooks/files/vault/{hostname}/` to target paths on hosts. Plaintext files are unencrypted; vault files are Ansible Vault encrypted.

## Key Patterns

- **Docker Swarm stacks** are defined entirely within `playbooks/host_vars/rackvm1/vault.yml` as the `vault_docker_stack_definition` variable. Services, volumes, networks, and all config live there. The stack is still named `rackpis` and every volume is `rackpis_*`; renaming it would point all 41 services at empty volumes, so the name stays regardless of which hosts run it.
- **Docker Compose** services for individual hosts are in their respective `host_vars/{hostname}/vault.yml` as `docker_compose_definition`.
- **Vault encryption** is enforced by a pre-commit hook (`hooks/pre-commit` → `make vault-check`). The `.vault_pass` file in the repo root (git-ignored) holds the encryption key.
- **Variable layering**: Role defaults → group_vars → host_vars. Vault variables are referenced indirectly (e.g., `docker_stack_definition: "{{ vault_docker_stack_definition }}"`).
- Services on the swarm are placed by node label (`node.labels.metrics == true`), not by hostname. A label is declared on exactly one host as a comma-delimited `docker_labels` in the `[docker_labels]` inventory group, and `default.yml` reconciles those onto the nodes. `portainer-agent` is global; nothing else pins to a host.
- Roles that install something follow a `present.yml` / `absent.yml` split routed from `tasks/main.yml` via a `<role>_state` variable — `absent` must fully reverse what `present` did (stop service, remove config, uninstall package, clear repo/keyring).
- Traefik labels on swarm services handle routing, OAuth middleware, and TLS.

## Moving a swarm workload to another host

Swarm does not move local volumes, so a workload moves in two halves: the data has to be copied while the services are stopped, and only then may the label follow. `generic/docker/migrate-labels` does both, driven by the inventory.

Move the label to the new host in `[docker_labels]`, then:

```bash
make run tags=docker-swarm ANSIBLE_FLAGS="-e docker_migrate_labels=yes"
```

Do not narrow this with `hosts=`: the role runs on the primary manager and reaches both nodes by delegation, so a limit has to name the source, the target and the manager, and omitting the manager means it never runs at all.

For each label whose inventory host no longer matches the node carrying it, the role scales that label's services to 0, tars each of their volumes through `docker_migrate_transit_dir` onto the target, removes the source copy, and moves the label. The stack deploy then starts the services on the target, on top of their data. Services and volumes are read from the stack definition, so nothing needs listing by hand.

Without `docker_migrate_labels=yes` the move is only reported, and the label reconcile is held back — moving a label while its data sits on the old host would start the service on an empty volume. A routine `make run` is therefore safe to run with a pending move outstanding; it just will not act on it.

Volumes are all the role moves. Check the rest by hand before moving a label, because a service that lands on a host missing any of it fails to start, or starts and is unreachable:

- **Host bind mounts** — `/var/docker/...` paths come from `project-files`, which is keyed by hostname, so the file tree and the `project_files` declaration move to the new host too.
- **CIFS shares** — the target needs whatever the services mount beyond `Backups`.
- **Locally built images** — `docker_images_to_build` moves with the workload, or the target has no image to run.
- **Host-network services** — these are not on the overlay, so traefik routes them from `external-rules.yaml` by address rather than discovering them. That address is not a label and does not follow the move; repoint it or the service is reachable locally and 502s through traefik.
- **Endpoints other hosts write to** — `influxdb3_url` follows the `metrics` label out of the inventory, but each telegraf agent only picks up the new address when its config is rewritten. Run `make run tags=telegraf` across every host after moving `metrics`, or the agents keep writing to the old one; nothing errors, the graphs just stop filling.
