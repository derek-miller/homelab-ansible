# Runbook

Operational procedures for running the fleet — moving workloads, applying
updates, host-specific bootstrap steps. This is what to know before *operating*
the infrastructure. For getting a dev environment working against this repo,
see [CONTRIBUTING.md](CONTRIBUTING.md); for repo conventions and gotchas
relevant to editing the Ansible itself, see [CLAUDE.md](CLAUDE.md).

## Moving a swarm workload to another host

Swarm does not move local volumes, so a move happens in two halves: copy the data while the services are stopped, and only then let the label follow. `generic/docker/migrate-labels` does both, driven by the inventory.

Move the label to the new host in `[docker_labels]`, then:

```bash
make run tags=docker-swarm ANSIBLE_FLAGS="-e docker_migrate_labels=yes"
```

**Do not narrow this with `hosts=`.** The role runs on the primary manager and reaches both nodes by delegation, so a limit has to name the source, the target *and* the manager; omit the manager and it never runs at all.

For each label whose inventory host no longer matches the node carrying it, the role scales that label's services to 0, tars each of their volumes through `docker_migrate_transit_dir` onto the target, removes the source copy, and moves the label. The stack deploy then starts the services on top of their data. Services and volumes are read from the stack definition, so nothing needs listing by hand.

Without `docker_migrate_labels=yes` the move is only reported and the label reconcile is held back, because moving a label while its data sits on the old host would start the service on an empty volume. A routine `make run` is therefore safe with a pending move outstanding. The hold-back is keyed on a fact set on the primary manager, and undefined holds rather than reconciles — so a `hosts=` limit can stop a move being detected, but cannot lift the interlock.

**Volumes are all the role moves.** Check the rest by hand first; a service that lands on a host missing any of it either fails to start or starts unreachable:

- **Host bind mounts** — `/var/docker/...` paths come from `project-files`, which is keyed by hostname, so the file tree and the `project_files` declaration move too.
- **CIFS shares** — the target needs whatever the services mount beyond `Backups`.
- **Locally built images** — `docker_images_to_build` moves with the workload, or the target has no image to run.
- **Host-network services** — not on the overlay, so Traefik routes them from `external-rules.yaml` by address rather than by discovery. That address is not a label and does not follow the move; repoint it or the service works locally and 502s through Traefik.
- **Endpoints other hosts write to** — `influxdb3_url` follows the `metrics` label out of the inventory, but each Telegraf agent only picks up the new address when its config is rewritten. Run `make run tags=telegraf` across every host after moving `metrics`, or the agents keep writing to the old address: nothing errors, the graphs just stop filling.

## Applying updates and rebooting pve1-5

Upgrading packages and rebooting are two opt-in flags on one play (`generic/proxmox-reboot`), not separate playbooks — but only one direction is independent. `proxmox_reboot_enabled` alone reboots without upgrading; `proxmox_upgrade_enabled` alone upgrades nothing, since the upgrade task lives inside this same role behind the same `proxmox_reboot_enabled` guard below. It still has an effect on its own, though: the final node-config reapply play is gated on `proxmox_upgrade_enabled` alone, so setting it without `proxmox_reboot_enabled` reapplies `generic/proxmox` config across all five nodes despite nothing having been upgraded. A routine update sets both:

```bash
make run-proxmox tags=proxmox-reboot ANSIBLE_FLAGS="-e proxmox_reboot_enabled=yes -e proxmox_upgrade_enabled=yes"
```

Drop `proxmox_upgrade_enabled` for a reboot with no package changes. `proxmox_reboot_enabled` must be passed every run — a tag alone can't gate it, since every tag runs unless `--tags` narrows the playbook, so the role's first task is a `meta: end_play` guard on that var.

**The play runs from `hosts: localhost` and delegates to each node in turn** — `serial`/`order` can't express a custom reboot order, so it loops once from the controller instead of running per-host. That means **`hosts=`/`--limit` does nothing to it**: `localhost` doesn't match a node-limited pattern, so the whole play is silently skipped rather than narrowed, same failure shape as the swarm-migration `hosts=` trap above. To reboot specific nodes, set the order directly:

```bash
make run-proxmox tags=proxmox-reboot ANSIBLE_FLAGS="-e proxmox_reboot_enabled=yes -e proxmox_reboot_order='[\"pve3\"]'"
```

Default order is `groups['proxmox']` (pve1-5).

Per node, in order: confirms the cluster is quorate and finds the node's running HA resources, both checked from a witness node since the node about to reboot can't answer for itself, then records which of its guests are currently running — checked on the node itself, since `qm list` is node-local and a witness would report its own guests, not the target's. It then protects the node's HA resources per `proxmox_reboot_ha_mode` below, applies the package upgrade, and reboots, waiting for the node to rejoin quorate and for its guests and HA resources to come back. The upgrade sits after the HA protection because PVE can't live-migrate a guest onto an older QEMU than it's running, so in `downtime` mode the node's HA guests are stopped across the upgrade; non-HA guests still run through it. The `always:` block that hands resources back to HA covers the upgrade too and runs even on failure, because a resource left `--state stopped` or a node left in maintenance both persist past a failed run rather than reverting on their own.

`proxmox_reboot_ha_mode` (default `downtime`) governs how the node's HA resources are protected during the reboot itself, not whether guests come back after:
- `downtime` — marks them `--state stopped` so HA doesn't fence them while the node is down; they resume in place once it's back.
- `migrate` — puts the node into HA maintenance first, which live-migrates its HA resources off before rebooting. Only wins if live migration is actually fast; two offline moves (off, then back) is usually slower than the reboot it was meant to avoid, which is why `downtime` is the default.

Non-HA guests are never started by this play — it only *waits* for guests that were running before the reboot to be running again, which only happens for guests with `onboot: yes`. That wait is not a soft check: it has no `ignore_errors`, so a guest that doesn't come back within `proxmox_reboot_guest_timeout` (default 300s) fails the task hard, loudly, in the output. That failure sits outside the per-node `block`/`always`, so it stops the whole rolling loop — every node still left in `proxmox_reboot_order` is skipped, even though the failed node's own HA handback already ran cleanly before the failure. Not reachable on the current fleet (every running non-HA guest on pve1-5 has `onboot: 1` today), but a new guest added without it will stall a rolling reboot partway through rather than being skipped over.

A final play reapplies `generic/proxmox` (`scope: node`) across the whole cluster whenever `proxmox_upgrade_enabled` was set, whether or not any node actually rebooted — a PVE upgrade can reset node-level settings the role manages, and reasserting them unconditionally afterward is simpler than detecting what an upgrade actually touched. That's true of the clean `meta: end_play` exit above, but not of a failure: the rolling loop runs as a single `localhost` play with no error-control override, so the hard failure described above aborts the whole playbook run on the spot — the reapply play never starts, not even for nodes that already upgraded before the failure.

## macOS hosts: manual steps at bootstrap

Some macOS state cannot be set from an Ansible run, and the reason differs per case. The playbook does not handle these uniformly, so read the bullet rather than assuming a failed run told you about the problem: `generic/smb-mount` halts on an interactive `pause:` with a 10-minute timeout, `generic/plex` only prints a `debug:` note and continues green, and FileVault has no detection at all.

- **autofs SMB map** — autofs only reads `/etc/auto_smb` when `/etc/auto_master` carries a `/-  auto_smb` direct-map line. Only an interactive local Terminal.app session can write that file: direct SSH write, a boot-time LaunchDaemon, and SSH with Full Disk Access granted to sshd all fail identically with EPERM. The role writes the map itself over SSH, then detects a missing `auto_master` line and pauses for 10 minutes with the exact command to paste. **macOS updates reset `/etc/auto_master` and strip the line, so after every OS update re-run `make run hosts=<host> tags=smb-mount` and be at the Mac to answer the prompt** — the re-run only detects the problem, it cannot fix it. Leave the prompt unanswered and the pause times out, then the verify task fails the play for that host.
- **Plex "Open at Login"** — the menu-bar app registers its own LaunchAgent via LSSharedFileList, which needs Automation TCC and so cannot be set programmatically. After `generic/plex` installs the cask, launch Plex once via VNC or console and tick the box. The role prints instructions until the LaunchAgent appears.
- **FileVault login on reboot** — after a power cycle the Mac sits at the login screen until a human logs in, because user LaunchAgents only run post-login. No workaround; design-accepted.

## Restoring volumes from a backup archive

`generic/docker/restore-volumes` reads a `offen/docker-volume-backup` archive off the `Backups` share and writes each `backup/<volume>/` directory it finds back into the matching docker volume. It is opt-in per host:

```bash
make run hosts=<host> tags=docker-restore-volumes ANSIBLE_FLAGS="-e docker_restore_volumes=yes"
```

Swarm nodes are drained for the duration and made active again afterward, so the services release their volumes while the data lands. The role picks the **newest archive by filename** in `<docker_volume_backup_root>/<label>` and never asks: `docker_restore_volumes_label` chooses the directory, and `docker_restore_volumes_only` narrows which volumes come out of it. A volume that already holds data is skipped unless `docker_restore_volumes_force=yes`, the only guard against overwriting live data.

**Encrypted archives are handled by suffix, not by configuration.** Turning on `AGE_PUBLIC_KEYS` for a backup service makes it write `backup-<ts>.tar.gz.age`; the role discovers both shapes and switches on the trailing `.age` of whichever archive is newest. A directory mid-cutover holds both, and the newest wins either way, so the cutover works in either direction.

Three consequences:

- **The private key is not on the fleet.** It lives vault-encrypted in the repo and is staged to the restore target at 0600 for the length of the run, check runs included, then removed in an `always` block, including when the run fails. A compromised node holds only the public key, so it cannot read any archive, including the ones it wrote itself. `age` is installed on demand on the target, from Debian stable.
- **Decryption is streamed, never written to disk.** The archive is bind-mounted from `/mnt/Backups`, so decrypting it in place would write the cleartext back onto the share the encryption is protecting. Nothing is written next to the archive.
- **Recovery needs the repo and the vault password.** Neither can live only on the fleet it protects. The archives on the NAS are recoverable exactly as far as that password is.

**A dry run of this role is not inert.** Listing an encrypted archive requires decrypting it, so `--check` (`make dry-run`, or the command above with `--check` appended) installs `age` on the target and stages the identity at 0600 exactly as a real run does, removing it afterward. Only the volume writes are simulated: the volume create, the extract, and the ownership fix.

The pipeline sets `pipefail`, so a wrong or missing identity fails the run at the listing step with age's own error. Without it a failed decrypt exits 0 through `tar` and the play reports a successful restore of nothing.
