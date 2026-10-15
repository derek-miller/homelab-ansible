# Runbook

Operational procedures for running the fleet — moving workloads, applying
updates, host-specific bootstrap steps. This is what to know before *operating*
the infrastructure. For getting a dev environment working against this repo,
see [CONTRIBUTING.md](CONTRIBUTING.md); for repo conventions and gotchas
relevant to editing the Ansible itself, see [CLAUDE.md](CLAUDE.md).

## Pausing deploys

Every merge to master runs `.github/workflows/deploy.yml`, which redeploys the stack and resets every service's replicas. During a restore or any other hand-run change, pause it with a repository variable:

```bash
gh variable set DEPLOY_PAUSED --body true -R derek-miller/homelab-ansible
```

Runs that start after that are skipped, but one already running or queued still runs. Wait for each one listed to finish before touching any service; cancelling can stop the playbook partway:

```bash
gh run list --workflow Deploy -R derek-miller/homelab-ansible --json databaseId,status --jq '.[] | select(.status != "completed") | .databaseId'
gh run watch <id> -R derek-miller/homelab-ansible
```

To resume, delete the variable and deploy whatever merged in the meantime:

```bash
gh variable delete DEPLOY_PAUSED -R derek-miller/homelab-ansible
gh workflow run Deploy -R derek-miller/homelab-ansible
```

A local `make run` ignores the variable.

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

## Upgrading Docker Engine on the swarm

A deploy installs Docker but never upgrades it. The `docker-upgrade` play does, one swarm node at a time, and only when asked for by tag. [Pause deploys](#pausing-deploys) for the whole run, then:

```bash
make run tags=docker-upgrade
```

It takes rackvm5 first and the managers last, and stops at the first failure. For each node it:

1. upgrades the Docker CLI plugins in place, which needs no restart;
2. drains the node through another manager, after checking that every node is ready, every manager is reachable and no other node is drained;
3. upgrades the engine packages (`docker-ce`, its CLI and rootless extras, `containerd.io`) and writes `daemon.json`;
4. restarts Docker and makes the node active once it has rejoined;
5. waits until every task placed back on it is running and then holds for 30 s with no task replaced. It allows up to six such rounds, so a service that restarts while its dependencies come up (oauth2-proxy while Keycloak starts) can settle, but a crash loop fails the run.

A node with nothing to upgrade and a matching `daemon.json` is skipped without being drained, so a stopped run can be run again. If it stopped with a node still drained, make that node active from a manager first, with `docker node update --availability active <node>`. A `hosts=` limit has to include a manager other than the node being upgraded. `make dry-run tags=docker-upgrade` reports what each node needs and stops before draining anything.

While a node is drained, the services pinned to it are down: a few minutes each, longer for influxdb3 and YouTrack. Everything behind Traefik is down while rackvm1 is drained, and containers started outside the stack are stopped with Docker. Run it outside the 01:00–05:30 UTC backup window. A service that cannot stay up anyway, such as zigbee2mqtt with its coordinator unplugged, fails step 5 and stops the run; leave it out with `ANSIBLE_FLAGS="-e '{\"docker_upgrade_ignore_services\":[\"zigbee2mqtt\"]}'"`.

The play writes `daemon.json` from the checkout it runs from. A deploy writes it from master and, whenever the two differ, restarts Docker on every node at once. So a `daemon.json` change goes in through this play: pause deploys, merge it, run the play from an up-to-date master, then resume deploys and run Deploy, which finds `daemon.json` already in place and restarts nothing.

A deploy refuses to write `default-stop-timeout` on an engine older than 29.7, where Docker would refuse to start. That check covers only this one key; an option that needs a newer engine needs its own clause in the assert at the top of the `docker` play.

## k3s beside Docker

k3s runs on the rackvms next to Docker while workloads move off Swarm: one server on rackvm1 (`[k3s_server]`) and agents in `[k3s_agent]`. Its data directory is `/mnt/data/k3s`, so like Docker it waits for the data disk at boot. A deploy installs it; to run just this part:

```bash
make run tags=k3s
```

The server goes first, then the agents one at a time, and each waits for its node to report Ready. kubectl runs as root on rackvm1: `sudo kubectl get nodes`.

**Upgrading.** Bump `k3s_version` and `k3s_sha256` in `generic/k3s/defaults/main.yml` together; the checksum is the `k3s` line of the release's `sha256sum-amd64.txt`. The deploy replaces the binary and restarts k3s, the server before the agents. Pods keep running through a k3s restart.

**Portainer.** The cluster is an Edge environment in Portainer, like the swarm and the Pis. The server writes the agent's manifest into k3s's `server/manifests`, which k3s applies and reapplies on change. Every run resolves `portainer/agent:latest` to its current digest, so a new agent release rolls out with the next deploy. To re-key it, add the environment again in Portainer (Kubernetes, Edge Agent Standard) and take the Edge ID and key from the command it shows, without running it. Replace `k3s_portainer_edge_id` and `k3s_portainer_edge_key` in `group_vars/k3s/vars.yml` with `ansible-vault encrypt_string` output. Clearing both removes the agent.

**Removing it from a node.** Remove agents before the server, since removing the server deletes the cluster's datastore:

```bash
make run hosts=rackvm5 tags=k3s ANSIBLE_FLAGS="-e k3s_state=absent"
```

That stops k3s, kills its pods, and removes its mounts, network namespaces, interfaces, iptables chains, ipsets, units, binary and data directory. Data a pod kept in a host path outside `/mnt/data/k3s` stays where it is. Then delete the node on rackvm1 with `sudo kubectl delete node rackvm5`, which also drops the password it registered with; until then the server refuses the same name from a reinstalled agent. Last, take the host out of its `k3s_*` group, or the next deploy installs it again. Sysctls set while it ran, the role's inotify limits and kube-proxy's `net.ipv4.conf.all.route_localnet` among them, stay until the next reboot.

Never run k3s's own `k3s-uninstall.sh` or `k3s-killall.sh` here. Besides removing k3s they clear the node's Tailscale advertised routes, which withdraws the NAS route rackvm1-5 carry, and pipe all of iptables through `iptables-restore` underneath Docker.

**Rebuilding the server.** The cluster's identity and state live in rackvm1's `/mnt/data/k3s/server`: the SQLite datastore, the cluster CA and the service-account keys. The token in `group_vars/k3s/vars.yml` unlocks that state but cannot recreate it.

- *rackvm1 restored from its replica or a vzdump.* Start it; k3s comes back as of the copy. A restored datastore rewinds the resource version, and a client watching from a later one can miss changes, so restart every agent and CoreDNS once the server answers:

  ```bash
  sudo kubectl -n kube-system rollout restart deploy/coredns
  ```

  and `sudo systemctl restart k3s-agent` on each agent.
- *No usable copy.* Run `make run hosts=rackvm1 tags=k3s` for a fresh server with the same token, then `make run tags=k3s` for the agents. An agent that will not rejoin is removed with `k3s_state=absent` and installed again. Everything the cluster ran is then redeployed from the repo; data on the nodes' disks is untouched.

## Applying updates and rebooting pve1-5

Upgrading packages and rebooting are two opt-in flags on one play (`generic/proxmox-reboot`), not separate playbooks — but only one direction is independent. `proxmox_reboot_enabled` alone reboots without upgrading; `proxmox_upgrade_enabled` alone upgrades nothing, since the upgrade task lives inside this same role behind the same `proxmox_reboot_enabled` guard below. It still has an effect on its own, though: the final node-config reapply play is gated on `proxmox_upgrade_enabled` alone, so setting it without `proxmox_reboot_enabled` reapplies `generic/proxmox` config across all five nodes despite nothing having been upgraded. A routine update sets both:

```bash
make run-proxmox tags=proxmox-reboot ANSIBLE_FLAGS="-e proxmox_reboot_enabled=yes -e proxmox_upgrade_enabled=yes"
```

Drop `proxmox_upgrade_enabled` for a reboot with no package changes. `proxmox_reboot_enabled` must be passed every run — a tag alone can't gate it, since every tag runs unless `--tags` narrows the playbook, so the role's first task is a `meta: end_play` guard on that var.

**The play runs from `hosts: localhost` and delegates to each node in turn** — `serial`/`order` can't express a custom reboot order, so it loops once from the controller instead of running per-host. That means **`hosts=`/`--limit` does nothing to it**: `localhost` doesn't match a node-limited pattern, so the whole play is silently skipped rather than narrowed, same failure shape as the swarm-migration `hosts=` trap above. To reboot specific nodes, set the order directly:

```bash
make run-proxmox tags=proxmox-reboot ANSIBLE_FLAGS="-e proxmox_reboot_enabled=yes -e '{\"proxmox_reboot_order\":[\"pve3\"]}'"
```

The order has to be passed as JSON. In `key=value` form the list arrives as a string and the loop fails before touching a node.

Default order is `groups['proxmox']` (pve1-5).

Per node, in order: confirms the cluster is quorate and finds the node's running HA resources, both checked from a witness node since the node about to reboot can't answer for itself, then records which of its guests are currently running — checked on the node itself, since `qm list` is node-local and a witness would report its own guests, not the target's. It then protects the node's HA resources per `proxmox_reboot_ha_mode` below, applies the package upgrade, and reboots, waiting for the node to rejoin quorate and for its guests and HA resources to come back. The upgrade sits after the HA protection because PVE can't live-migrate a guest onto an older QEMU than it's running, so in `downtime` mode the node's HA guests are stopped across the upgrade; non-HA guests still run through it. The `always:` block that hands resources back to HA covers the upgrade too and runs even on failure, because a resource left `--state stopped` or a node left in maintenance both persist past a failed run rather than reverting on their own.

`proxmox_reboot_ha_mode` (default `downtime`) governs how the node's HA resources are protected during the reboot itself, not whether guests come back after:
- `downtime` — marks them `--state stopped` so HA doesn't fence them while the node is down; they resume in place once it's back.
- `migrate` — puts the node into HA maintenance first, which live-migrates its HA resources off before rebooting. Only wins if live migration is actually fast; two offline moves (off, then back) is usually slower than the reboot it was meant to avoid, which is why `downtime` is the default.

Non-HA guests are never started by this play — it only *waits* for guests that were running before the reboot to be running again, which only happens for guests with `onboot: yes`. That wait is not a soft check: it has no `ignore_errors`, so a guest that doesn't come back within `proxmox_reboot_guest_timeout` (default 300s) fails the task hard, loudly, in the output. That failure sits outside the per-node `block`/`always`, so it stops the whole rolling loop — every node still left in `proxmox_reboot_order` is skipped, even though the failed node's own HA handback already ran cleanly before the failure. Not reachable on the current fleet (every running non-HA guest on pve1-5 has `onboot: 1` today), but a new guest added without it will stall a rolling reboot partway through rather than being skipped over.

A final play reapplies `generic/proxmox` (`scope: node`) across the whole cluster whenever `proxmox_upgrade_enabled` was set, whether or not any node actually rebooted — a PVE upgrade can reset node-level settings the role manages, and reasserting them unconditionally afterward is simpler than detecting what an upgrade actually touched. That's true of the clean `meta: end_play` exit above, but not of a failure: the rolling loop runs as a single `localhost` play with no error-control override, so the hard failure described above aborts the whole playbook run on the spot — the reapply play never starts, not even for nodes that already upgraded before the failure.

## Volume backup freshness monitoring

`offen/docker-volume-backup` emits no metrics, and its `NOTIFICATION_URLS` only fire on a run that actually happens — a container whose cron has stopped is silent and looks healthy. So the monitoring watches the *outcome* on the share rather than the process: `generic/volume-backup-stats` runs a Telegraf `filecount` input over `/mnt/Backups/Volumes/*` and reports `count` and `newest_file_timestamp` per target directory.

The glob is load bearing. `filecount` emits **one series per entry in `directories`**, and `recursive` only controls how deep it walks to build that one aggregate — so pointing it at the root with `recursive = true` yields a single series whose `newest_file_timestamp` is whichever target ran most recently. Every stalled target hides behind a healthy one. Globbing the children instead gives one series per target and is layout agnostic, so a new backup service needs no monitoring change.

Every backup container, on the swarm and on the Pis, also posts failed runs to the Homelab Backups Discord webhook through `NOTIFICATION_URLS` (`docker_volume_backup_notification_urls`, vaulted in `group_vars/all`). That covers a run that starts and fails, including a copy to the share that breaks off partway; the rules below cover a run that never starts.

It runs on one host (`[volume-backup-stats]`, currently `rackvm4`), since every rackvm mounts the same share and 14 duplicate copies of the same numbers buy nothing.

Anything placed under `docker_volume_backup_root` is monitored, unconditionally. There is no exclusion list and no name matching, which is what keeps a new backup target from needing a monitoring change to be covered — and what stops a rename from silently dropping one out of coverage.

That is why retired backups go in `/mnt/Backups/RetiredVolumes/`, a sibling of `Volumes` rather than a child. A folder of deliberately kept archives inside the monitored root would sit at `count > 0` with an old timestamp forever, which is a `Volume Backup Stale` alert that can never clear. Restoring from one needs `docker_restore_volumes_backup_dir` set directly: `generic/docker/restore-volumes` derives its path as `<docker_volume_backup_root>/<label>`, so `docker_restore_volumes_label` cannot reach outside the monitored root.

A **decommissioned host** is the mirror case and takes the opposite handling: its target directory is left empty with nothing worth preserving, so it belongs in neither place and wants removing outright. Use `rmdir` rather than `rm -rf` — the check and the delete are then the same syscall, so a directory that still holds archives refuses to be removed instead of being destroyed, and that refusal is the signal that the case was a retirement after all.

Skipping either cleanup fails quietly in the same way: kept archives inside the monitored root are a permanent `Volume Backup Stale`, an emptied target is a permanent `Volume Backup Empty`, and both alerts are correct — there is nothing to fix but the directory, which is what turns them into background noise. Time the removal against the collector rather than against the alert. A target removed while its samples still read healthy never reports a problem at all; one left in place until it empties out or goes stale pages first, and then goes on reading that way until its last sample ages out of the rule's 15 minute window. The same ordering applies to a directory the collector has not scraped yet, which is why a dead target found during a monitoring rollout should be cleared before the rules are unpaused rather than after.

Three alert rules, in the Grafana rule group `backup`, because one threshold does not cover the failure modes. This repo provisions no Grafana alerting, so they are configured in Grafana itself and appear nowhere in this tree:

- **Volume Backup Stale** — `newest_file_timestamp` older than 2 days, filtered to `count > 0`. Retention is 7 daily archives, so 2 days is clear of a single missed run. The `count > 0` filter matters: `filecount` reports `newest_file_timestamp = 0` for a directory with no matching files, which reads as epoch 1970 and would otherwise pin that target to a permanent stale alert.
- **Volume Backup Empty** — `count == 0`, the case the staleness rule excludes. Catches a target that has never produced an archive, or whose whole retention window has aged out.
- **Volume Backup Metrics Absent** — no series at all. An unmounted share emits *nothing* rather than something stale, so silence and health look identical to the first two rules. `Host Deadman` already covers rackvm4 being down, but not Telegraf running fine on a host whose CIFS mount has dropped.

What none of this catches: a backup target directory being **deleted** disappears from the glob silently, the same shape as the absent case but scoped to one target. A pruned service leaves its directory behind, so the ordinary stop-producing case is covered; only an actual `rm -rf` of the target is not.

Nor does it catch a *partial* archive. `offen` builds the archive under `/tmp` inside the container and then byte-copies it onto the share under its final name, and it does not unlink the destination if that copy dies partway (`internal/storage/local/local.go`: `os.Create` then `io.Copy`, no rename anywhere in the tree). A copy interrupted by a dropped mount leaves a truncated `backup-<ts>.tar.gz` carrying a current mtime, which reads as fresh. Freshness here means an archive arrived, not that it is complete or restorable.

YouTrack writes its own scheduled backups into the `youtrack-backups` volume (Database Backup settings: location `/opt/youtrack/backups`, TAR.GZ, keep 2), and `backup_youtrack` archives that volume with the rest. Telegraf cannot read inside node-local volumes, so `youtrack-backup-check` watches it instead: it exits after 60 s whenever no `*.tar.gz` there is newer than 26 h, and a stopped schedule therefore shows as **Container Crash Loop (Swarm)** for that service.

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

Four consequences:

- **The private key is not on the fleet.** It lives vault-encrypted in the repo and is staged to the restore target at 0600 for the length of the run, check runs included, then removed in an `always` block, including when the run fails. A compromised node holds only the public key, so it cannot read any archive, including the ones it wrote itself. `age` is installed on demand on the target, from Debian stable.
- **Decryption is streamed, never written to disk.** The archive is bind-mounted from `/mnt/Backups`, so decrypting it in place would write the cleartext back onto the share the encryption is protecting. Nothing is written next to the archive.
- **Recovery needs the repo and the vault password.** Neither can live only on the fleet it protects. The archives on the NAS are recoverable exactly as far as that password is.
- **Rotating the keypair does not reach the archives already written.** Each one stays readable only by the identity it was encrypted to, so a retired key has to be kept until the last archive naming it as recipient ages out of the 7 day retention.

**A dry run of this role is not inert.** Listing an encrypted archive requires decrypting it, so `--check` (`make dry-run`, or the command above with `--check` appended) installs `age` on the target and stages the identity at 0600 exactly as a real run does, removing it afterward. Only the volume writes are simulated: the volume create, the extract, and the ownership fix.

The pipeline sets `pipefail`, so a wrong or missing identity fails the run at the listing step with age's own error. Without it a failed decrypt exits 0 through `tar` and the play reports a successful restore of nothing.

## Restoring one volume from a VM backup

The nightly vzdump of each rackvm (VMIDs 201-205 for rackvm1-5, 03:30 America/Chicago, to the `unas-backup` storage) includes the data disk, so any volume can come back from it, including ones no archive covers. Extract on a pve node, which mounts the dumps; nothing is restored as a VM:

```bash
F=/mnt/pve/unas-backup/dump/vzdump-qemu-<vmid>-<timestamp>.vma.zst
D=$(mktemp -d /var/tmp/vzrestore.XXXX); mkdir "$D/m"
zstd -q -d -c "$F" | vma extract - "$D/x"
mount -o ro,loop,noload "$D/x/disk-drive-scsi1.raw" "$D/m"
tar -C "$D/m/docker/volumes" -czf /mnt/pve/unas-backup/restore-<volume>.tgz rackvms_<volume>/_data
umount "$D/m"; rm -rf "$D"
```

- **Do not pass `-r` to `vma extract`.** That option reads a device map from a FIFO, so the extract waits on it forever and writes nothing.
- **Do not use `qmrestore`.** It creates a second VM with the same MAC, address and `onboot`.
- **Scratch space is small.** The raw images are sparse and the pool compresses zeros; rackvm1's took 66 s and about 5.4 GiB.

The archive appears on the rackvms as `/mnt/Backups/Proxmox/restore-<volume>.tgz`, outside the monitored root. Put it back with [deploys paused](#pausing-deploys), since a deploy resets replicas:

```bash
docker service scale rackvms_<service>=0
V=/mnt/data/docker/volumes/rackvms_<volume>
mv "$V/_data" "$V/_data.pre-restore" && mkdir "$V/_data"
tar -C "$V" --strip-components=1 -xzpf /mnt/Backups/Proxmox/restore-<volume>.tgz rackvms_<volume>/_data
docker service scale rackvms_<service>=1
```

Run `docker service scale` on a manager and the rest on the node that holds the volume. Once the service works, remove `_data.pre-restore` and the `.tgz`. A Postgres data directory from a VM backup runs crash recovery on its first start; that is expected.

| Date | VM backup | Volumes | Result |
|---|---|---|---|
| 2026-10-14 | 201 (rackvm1), 03:30 | `keycloak-pg-data`, `portainer-ee-data`, `letsencrypt` | Extracted to a scratch copy, not swapped in. Postgres recovered and was ready 11 s after unpacking; Keycloak's realm, client, user, credential and role counts matched production; `acme.json` parsed. |

## Restoring a Postgres database from its dump

`keycloak-pg-dump` and `sourcebot-pg-dump` dump their database every hour at :15 into their own volume, as `keycloak.sql.gz` and `sourcebot.sql.gz` (plain SQL, gzip). `backup_identity` and `backup_sourcebot` archive those volumes nightly, so the last hour comes from the volume and an earlier night from the archive.

A dump service waits up to 5 min for its database when it starts. On any failure it exits after 60 s, which shows as **Container Crash Loop (Swarm)**, and `docker service logs rackvms_<db>-pg-dump` says which: `pg_dump failed`, `postgres not ready after 5 min`, or `dump shrank from <old> to <new> bytes, previous dump kept`. A dump under half the previous size is refused, so a real shrink (expired Keycloak events or sessions, repos removed from Sourcebot) loops until you accept it. On the node with that label, move the old dump aside and delete it once a new one is in place:

```bash
mv /mnt/data/docker/volumes/rackvms_<db>-pg-dump/_data/<db>.sql.gz /root/<db>.sql.gz.pre-shrink
```

### Loading a dump

[Pause deploys](#pausing-deploys) throughout, since a deploy resets replicas. A dump service dumps as soon as it starts, so it stays at 0 until the load has succeeded; otherwise it replaces the dump you are loading. For Keycloak, on a manager:

```bash
docker service scale rackvms_keycloak=0 rackvms_keycloak-pg-dump=0
```

Then on the node with the `identity` label. The chain stops at the first failure: a damaged dump leaves the database untouched, and `keycloak_pre_restore` must not already exist.

```bash
set -o pipefail
F=/mnt/data/docker/volumes/rackvms_keycloak-pg-dump/_data/keycloak.sql.gz
C=$(docker ps -qf name=rackvms_keycloak-postgres)
gunzip -t "$F" &&
  docker exec $C psql -X -v ON_ERROR_STOP=1 -U keycloak -d postgres -c 'ALTER DATABASE keycloak RENAME TO keycloak_pre_restore' -c 'CREATE DATABASE keycloak OWNER keycloak TEMPLATE template0' &&
  gunzip -c "$F" | docker exec -i $C psql -X -q -v ON_ERROR_STOP=1 --single-transaction -U keycloak -d keycloak >/dev/null
```

If the load fails, the new database is empty, since the load is one transaction. Swap the names back; both renames run as one transaction, so nothing changes unless both succeed:

```bash
docker exec $C psql -X -v ON_ERROR_STOP=1 -U keycloak -d postgres -c 'ALTER DATABASE keycloak RENAME TO keycloak_failed_restore; ALTER DATABASE keycloak_pre_restore RENAME TO keycloak'
```

Then bring up only Keycloak with `docker service scale rackvms_keycloak=1`, leave the dump service at 0, fix the cause and retry.

Once the load succeeds, scale both back up on a manager with `docker service scale rackvms_keycloak=1 rackvms_keycloak-pg-dump=1`. Once logins work, drop the kept copies:

```bash
docker exec $C psql -U keycloak -d postgres -c 'DROP DATABASE IF EXISTS keycloak_pre_restore' -c 'DROP DATABASE IF EXISTS keycloak_failed_restore'
```

Sourcebot is the same with `sourcebot` in place of `keycloak`, on the node with the `sourcebot` label.

### Loading an earlier night's dump

Scale both services to 0 as above first, then put the dump volume back from its label's archive and load it as above, skipping the scale-down. The list has to be JSON, as with `proxmox_reboot_order`:

```bash
make run hosts=rackvm1 tags=docker-restore-volumes ANSIBLE_FLAGS="-e docker_restore_volumes=yes -e docker_restore_volumes_label=identity -e docker_restore_volumes_force=yes -e '{\"docker_restore_volumes_only\":[\"keycloak-pg-dump\"]}'"
```

That takes the newest archive, which is last night's. For an earlier one, also pass `-e '{\"docker_restore_volumes_archive_patterns\":[\"backup-<ts>.tar.gz.age\"]}'` with the file name from `/mnt/Backups/Volumes/identity`, and check the `Restoring from:` line.

The run drains every host in `hosts=`. rackvm1 also carries `edge`, so everything behind Traefik is down while it runs. Sourcebot's node is a worker, so its run needs a manager in `hosts=` as well, with label `sourcebot` and `[\"sourcebot-pg-dump\"]`. That manager is drained too and is left with its own `rackvms_sourcebot-pg-dump`; remove it there afterwards with `docker volume rm rackvms_sourcebot-pg-dump`.

### Drills

Each drill loads the dump from the volume into a scratch Postgres (production's image) on an `--internal` Docker network on the database's own node, using the load chain above. To check Keycloak itself, start production's Keycloak image against the copy with `KC_PROXY_HEADERS=xforwarded` and request each realm's `/.well-known/openid-configuration` with `X-Forwarded-Proto: https`. Every realm requires HTTPS, so plain HTTP gets 403.

| Date | Dump | Result |
|---|---|---|
| 2026-10-14 | `keycloak.sql.gz`, 21:06 | Loaded in 5 s. Realm, client, user, credential, role and component counts and schema version 26.8.1 matched production. Keycloak started on it in 8.5 s, and all three realms answered 200. |
| 2026-10-14 | `sourcebot.sql.gz`, 21:15 | Loaded in 4 s. All 37 tables matched production's row counts. |

## docker-volume-backup on a Pi is unhealthy

`raspi1`, `gianos-raspi1`, `quinn-raspi1` and `mertins-raspi1` each run an `offen/docker-volume-backup` container on a `0 0 * * *` cron, writing to `/mnt/Backups/Volumes/<host>` over the `unas_over_ts` CIFS mount. The container's healthcheck asserts that directory holds a `backup-*` file newer than 26h, so the **Container Unhealthy** alert is what pages when a nightly stops landing.

**The alert cannot tell you why.** A wedged PID 1, a dropped CIFS mount and a genuinely missed cron all produce an identical unhealthy container. Work through them in this order, from a second Pi so a poisoned mount on the failing host cannot hang the investigation too:

1. **Is the archive actually stale?** `ls -l /mnt/Backups/Volumes/<host>/` from any other Pi. If the newest `backup-*` is under a day old, the archives are fine and the failing host's own view of the share is the suspect.
2. **Is the mount alive on the failing host?** `timeout 10 ls /mnt/Backups/Volumes/<host>/`. Hanging rather than erroring means the mount is poisoned, and the process doing the listing is now in uninterruptible sleep where `kill -9` cannot reach it. `ps -eo stat,pid,cmd | awk '$1 ~ /^D/'` lists everything already stuck there, and a load average pinned to exactly that count is the same signal read another way. Only a reboot clears D state.
3. **Is the container's PID 1 alive?** `docker top docker-volume-backup`. A defunct process, or a `docker exec` that reports the container stopped while `docker ps` shows it up, is the wedge this healthcheck exists to catch.
4. **Force a run.** `docker exec docker-volume-backup backup`. A complete run logs Stored, Pruned and Removed; one that stops after Stored died mid-cycle.

**Nothing restarts the container for you.** `restart: unless-stopped` reacts to process exit, not to health state, so an unhealthy container stays unhealthy until someone intervenes. It cannot be relied on across a reboot either: on 2026-08-11 a wedged container exited 0 during shutdown with `restartCount=0` and stayed down. Recovery is a reboot followed by `make run hosts=<host> tags=docker-compose`, not a container restart; the container's recorded compose working directory is an Ansible temp path that no longer exists, so `docker compose up` from there also fails.

**Expect the first successful run to prune hard.** Retention is 7 days and pruning is done by the same process that wedged, so a long outage leaves every archive past the window and the first completed run deletes them together. Recovering gianos went from 8 archives to 1 in a single run. There is no rollback depth until seven nightlies accumulate, which is worth saying out loud before anyone treats the recovery as finished.

**Detection takes about 28 hours.** 26h of staleness window, plus up to two more hours for two failed checks an hour apart, plus the alert's own pending period. A Friday night failure surfaces on Sunday morning. That is deliberate for a daily backup, but it does mean this is not a same-day signal.

**A fresh host reports `starting`, not `unhealthy`.** `start_period` is 27h, longer than the backup interval, so a newly built Pi is not counted as failing before its first archive can exist. The start period ends at the first passing check rather than at the 27h mark, so any container that has completed one backup reports `healthy` or `unhealthy` and nothing else: `starting` on a host that has been up for days is not benign.

## GitHub Actions runners for control4-zigbee3

`gha-runner-zigbee3` in the swarm stack runs one ephemeral GitHub Actions runner on every node except the one carrying `edge`, registered to `finitelabs/control4-zigbee3` under the node's hostname with the label `rack`. A task registers at start, takes one job and exits, and swarm starts a fresh task from the image, so no job inherits another's workspace. A task stopped while idle leaves an offline registration behind until the node's next task replaces it by name. The image is `gha-runner`, built on each runner node from `generic/docker/build-images/files/gha-runner/`.

**One limit fits every node.** Swarm sets `cpus` and `memory` per service, not per node, and has no CPU shares, so every runner gets the same hard cap: 4 CPUs and 8G, half of an 8-core rackvm. That is also why the edge node is left out. It has 4 cores, so a cap it could afford would halve every other runner, and it carries the ingress every routed service depends on. Node's `os.availableParallelism()` reads the CPU cap, so a job that sizes its workers by core count starts 4 of them rather than 8, and its memory follows.

**The token.** Each task trades `gha_runner_zigbee3_github_token` (`playbooks/group_vars/all/vars.yml`) for a one-hour registration token, then drops it from its environment before taking a job, since jobs inherit the runner's environment. It is a fine-grained personal access token. To set or rotate it:

1. On GitHub, Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token.
2. Resource owner `finitelabs`. Repository access: Only select repositories, `finitelabs/control4-zigbee3`.
3. Repository permissions: Administration, Read and write. GitHub adds Metadata, Read-only; grant nothing else.
4. Expiration: the longest the organization allows. A lapsed token stops new registrations (below), so note the date.
5. If `finitelabs` requires approval for fine-grained tokens, approve the request under the organization's Settings > Personal access tokens > Pending requests.
6. Copy the token, then from the repo root inside `.venv`:

   ```bash
   pbpaste | tr -d '\n' | ansible-vault encrypt_string --vault-id=.vault_pass --stdin-name gha_runner_zigbee3_github_token
   ```

   Replace the `gha_runner_zigbee3_github_token: !vault |` block in `playbooks/group_vars/all/vars.yml` with the output and commit.
7. Merging to master deploys it. The new value changes the service, so swarm replaces every task and cancels any job in flight; merge while the runners are idle.

**A bad or expired token** shows in the task log as `no registration token for finitelabs/control4-zigbee3` followed by GitHub's message, such as `Bad credentials`. The task waits a minute before exiting, which keeps the API calls down while still letting swarm's restarts trip **Container Crash Loop (Swarm)**. Every job is a new task, so that alert can also fire on a run of jobs under about three minutes each; the catalogue job takes far longer.

**Updating the runner.** Bump the `FROM` tag in the Dockerfile and `image_tag` together, since a node builds only a tag it does not have. An image that falls behind a runner release keeps working: each new task's runner updates itself first, at the cost of that download per job until the image catches up. The image is excluded from the docker-prune sweep, because between jobs no container may reference it, so after a bump remove the old tag by hand with `docker rmi gha-runner:<old>` on each runner node.

**If `edge` moves**, the runner goes the other way: it starts on the old edge node and leaves the new one. Move `gha-runner` in `docker_images_to_build` to match, and check the node it lands on can spare 4 CPUs and 8G.
