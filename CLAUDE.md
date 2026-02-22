# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Ansible-managed homelab infrastructure. Docker Swarm cluster (rackpi1 manager + rackmini1/rackpi2-5 workers) running on Raspberry Pi and Mac Mini nodes. Single main playbook (`playbooks/default.yml`) with tag-based execution.

## Common Commands

```bash
make after-git-pull     # Install all deps after pulling
make run                # Run default playbook against all hosts
make run hosts=rackpi1  # Limit to specific host
make run tags=docker    # Run specific tag (auto-skips base,common)
make dry-run            # Check mode
make check              # Syntax validation

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

- **Docker Swarm stacks** are defined entirely within `playbooks/host_vars/rackpi1/vault.yml` as the `vault_docker_stack_definition` variable. Services, volumes, networks, and all config live there.
- **Docker Compose** services for individual hosts are in their respective `host_vars/{hostname}/vault.yml` as `docker_compose_definition`.
- **Vault encryption** is enforced by a pre-commit hook (`hooks/pre-commit` → `make vault-check`). The `.vault_pass` file in the repo root (git-ignored) holds the encryption key.
- **Variable layering**: Role defaults → group_vars → host_vars. Vault variables are referenced indirectly (e.g., `docker_stack_definition: "{{ vault_docker_stack_definition }}"`).
- Services on the swarm use placement constraints like `node.hostname == rackmini1` to pin to specific nodes.
- Traefik labels on swarm services handle routing, OAuth middleware, and TLS.