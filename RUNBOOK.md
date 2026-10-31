# Runbook

Operational procedures for running the fleet — moving workloads, applying
updates, host-specific bootstrap steps. This is what to know before *operating*
the infrastructure. For getting a dev environment working against this repo,
see [CONTRIBUTING.md](CONTRIBUTING.md); for repo conventions and gotchas
relevant to editing the Ansible itself, see [CLAUDE.md](CLAUDE.md).

## Pausing deploys

Every merge to master runs `.github/workflows/deploy.yml`, as does a weekly run on Mondays at 14:00 UTC, which redeploys the stack and resets every service's replicas. During a restore or any other hand-run change, pause it with a repository variable:

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

**Skipping a host that is down.** An unreachable host fails every deploy. List it, or several separated by commas, in another repository variable, and deploys leave it out until you delete it:

```bash
gh variable set DEPLOY_SKIP_HOSTS --body raspi1 -R derek-miller/homelab-ansible
gh variable delete DEPLOY_SKIP_HOSTS -R derek-miller/homelab-ansible
```

Skipping the first swarm manager (rackvm1) skips the stack deploy too, which runs from it. The next deploy after deleting the variable brings a skipped host up to date. A local `make run` ignores this variable too.

## Moving a swarm workload to another host

Swarm does not move local volumes, so a move happens in two halves: copy the data while the services are stopped, and only then let the label follow. `generic/docker/swarm` does both, driven by the inventory, and the label's k3s apps move first in the same run (see [Moving a label](#apps-on-k3s) under Apps on k3s).

[Pause deploys](#pausing-deploys) for the move. Move the label to the new host in `[docker_labels]`, then:

```bash
make run tags=docker-swarm ANSIBLE_FLAGS="-e docker_migrate_labels=yes"
```

**Do not narrow this with `hosts=`.** The migration runs on the primary manager and reaches both nodes by delegation, so a limit has to name the source, the target *and* the primary manager, and it stops if the primary is missing.

For each label whose inventory host no longer matches the node carrying it, the role scales that label's services to 0, tars each of their volumes through `docker_migrate_transit_dir` onto the target, removes the source copy, and moves the label. The stack deploy then starts the services on top of their data. Services and volumes are read from the stack definition, so nothing needs listing by hand.

Without `docker_migrate_labels=yes` the move is only reported and the label reconcile is held back, because moving a label while its data sits on the old host would start the service on an empty volume. A routine `make run` is therefore safe with a pending move outstanding. The move is worked out from every node in the swarm after any new node has joined, so a `hosts=` limit cannot hide one.

**Volumes are all the role moves.** Check the rest by hand first; a service that lands on a host missing any of it either fails to start or starts unreachable:

- **Host bind mounts** — `/var/docker/...` paths come from `project-files`, which is keyed by hostname, so the file tree and the `project_files` declaration move too.
- **CIFS shares** — the target needs whatever the services mount beyond `Backups`.
- **Locally built images** — `docker_images_to_build` moves with the workload, or the target has no image to run. The old host keeps its `<name>:latest` until you `docker rmi` it.
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

**Upgrading.** `k3s_version` in `generic/k3s/defaults/main.yml` names a k3s release channel, `stable` by default, and each deploy moves the server to that channel's release, at most one minor at a time since Kubernetes can't skip one. Agents install whatever the server runs. The binary is checked against the release's `sha256sum-amd64.txt`, then k3s restarts, the server before the agents; pods keep running through a k3s restart. To hold back, set `k3s_version` to a minor channel such as `v1.36` or an exact release such as `v1.36.5+k3s1`. The deploy refuses any release that would go back a minor or skip one, exact or not.

**Removing it from a node.** Remove agents before the server, since removing the server deletes the cluster's datastore. To remove the whole cluster, remove Flux first, as in [Apps on k3s](#apps-on-k3s), then the agents, then the server.

```bash
make run hosts=rackvm5 tags=k3s ANSIBLE_FLAGS="-e k3s_state=absent"
```

That stops k3s, kills its pods, and removes its mounts, network namespaces, interfaces, iptables chains, ipsets, units, binary and data directory; on the server, also Flux's registry volume and CLI. Other data a pod kept in a host path outside `/mnt/data/k3s` stays where it is. Then delete the node on rackvm1 with `sudo kubectl delete node rackvm5`, which also drops the password it registered with; until then the server refuses the same name from a reinstalled agent. Last, take the host out of its `k3s_*` group, or the next deploy installs it again. Sysctls set while it ran, the role's inotify limits and kube-proxy's `net.ipv4.conf.all.route_localnet` among them, stay until the next reboot.

Never run k3s's own `k3s-uninstall.sh` or `k3s-killall.sh` here. Besides removing k3s they clear the node's Tailscale advertised routes, which withdraws the NAS route rackvm1-5 carry, and pipe all of iptables through `iptables-restore` underneath Docker.

**Rebuilding the server.** The cluster lives in rackvm1's `/mnt/data/k3s/server`, on its data disk: the SQLite datastore, which also holds the cluster CA, the service-account keys and the key Secrets are encrypted with, sealed with the token in `k3s_cluster_token` (`host_vars/rackvm1/vars.yml`). A copy of that datastore plus the token brings back the same cluster; the token alone cannot. Proxmox copies rackvm1 (vm:201) to pve2 every 15 minutes and backs it up nightly, and it is an HA resource kept to pve1 and pve2, like vm:100. It and rackvm2 are sized, in their `host_vars`, so pve2 can take it and vm:100 at once when pve1 fails. HA keeps it on pve1 whenever pve1 is up and refuses `ha-manager migrate vm:201 pve2`; to move it by hand, make the rule's priorities equal with `ha-manager rules set node-affinity vm201-nodes --nodes pve1,pve2`, and `make run-proxmox tags=proxmox-cluster` puts them back, after which HA moves it back to pve1 itself. In order of preference:

- *pve1 fails.* HA starts rackvm1 on pve2 from its copy, at most 15 minutes old unless replication had stalled (`pvesr status` shows the last sync), and moves it back once pve1 returns. Then treat it as a restored copy, below.
- *rackvm1 restored from its replica or a vzdump.* To restore a vzdump, take it out of HA's hands with `ha-manager set vm:201 --state disabled`, run `qmrestore <newest vzdump-qemu-201 archive> 201 --force` on its node, then `ha-manager set vm:201 --state started`. k3s comes back as of the copy. A restored datastore rewinds the resource version, and a client watching from a later one can miss changes, so restart every agent and CoreDNS once the server answers:

  ```bash
  sudo kubectl -n kube-system rollout restart deploy/coredns
  ```

  and `sudo systemctl restart k3s-agent` on each agent. A label moved since the copy was taken has its volumes on the old node again in the copy, and the move renamed their folders there to `data.moved-*`: rename them back to `data` before the suite starts, or it starts empty.
- *No usable copy.* Pause deploys, then:
  1. Make a new server, with the same token but a new CA, with `make run hosts=rackvm1 tags=k3s ANSIBLE_FLAGS="--skip-tags=flux"`. Flux waits for the agents: with it, the run waits 11 minutes on the apps of agents that haven't rejoined, then fails. Before that run:
     - If rackvm1 itself is lost, bring the host back as in [Bringing up a new host](#bringing-up-a-new-host), rebuilt under the same name, with another manager in `hosts=` and `ANSIBLE_FLAGS="--skip-tags=k3s"` on each run. Those runs end failed on the image push until step 3 brings back the registry's route. Then check `docker network inspect docker_gwbridge` shows 172.19.0.0/16, the range edge's Traefik is allowed to reach the Swarm Traefik through. A fresh Docker may pick another.
     - If its datastore is still on disk but unusable, clear it with `make run hosts=rackvm1 tags=k3s ANSIBLE_FLAGS="-e k3s_state=absent"`.
  2. Run `sudo systemctl restart k3s-agent` on each agent. Until then they trust the old CA; after it they rejoin within seconds and stop the pods the new server doesn't know.
  3. `make run tags=k3s` then brings back everything the cluster ran from the repo. Volumes on rackvm2-5 are reused where they are, as are rackvm1's if its data disk survived. `sudo k3s kubectl get csinode` should list `smb.csi.k8s.io` on every node within a minute; for one that doesn't, delete its `csi-smb-node` pod in `kube-system`.
  4. Suites whose volumes were on a lost rackvm1 start empty. Restore each, with `-e k3s_restore_before=<time of the loss>`, before its next backup would save the empty one.

**Drills.** Rehearsing on rackvm1 would take every k3s route down, so the copy is checked in isolation and the steps on a throwaway cluster: two VMs on pve1 built from the repo, with a copy of `playbooks/hosts` whose `[k3s_server]` and `[k3s_agent]` name only them, plus their own token and app tree. Appending them to those groups instead would join them to this cluster.

| Date | Drill | Result |
|---|---|---|
| 2026-10-30 | rackvm1's 03:30 vzdump restored to a spare VMID on pve1, its network link down | Restored in 1 min 46 s. k3s started on the copy with this cluster's CA and service-account keys; namespace, volume, Secret, chart and Schedule counts matched, and every Secret decrypted. With the link down eth0 got no address, so the node IP was added by hand in the guest. |
| 2026-10-30 | Throwaway: server disk snapshot, cluster changed, server crash-stopped and rolled back | API back 13 s after start. Within 70 s the agent dropped the pods made after the snapshot and ran the older one again; services, DNS and Flux stayed current, before and after the restarts above. |
| 2026-10-30 | Throwaway: server's k3s removed with `k3s_state=absent`, then steps 1-3 | New server in 53 s, agent back 20 s after its restart, apps back in 4 min on all their old volumes, the server's included. |
| 2026-10-30 | Throwaway: server VM destroyed and cloned again, then steps 1-4 | The throwaway has no Swarm, so step 1 was one `make run`, which, with Flux, waited 11 min on the apps and failed; step 1 now leaves Flux out. Steps 2-3 took 2.5 min. The agent's NAS driver then had no registration until its pod was deleted by hand; the registrar probe added since put a removed socket back in 27 s. The server's suite started empty, and its restore, from a repository on the agent's disk rather than the NAS, took 37 s. |

**Rotating the token.** On rackvm1, `sudo k3s token rotate --data-dir /mnt/data/k3s --token <old> --new-token <new>`, then put the new token in `k3s_cluster_token` and run `make run tags=k3s` straight away, which restarts every node with it, the server first. An agent that restarts in between can't rejoin. A copy of rackvm1 from before the rotation unlocks only with the old token.

## Apps on k3s

Apps on the cluster are plain Kubernetes YAML under `kubernetes/`, which Flux applies. Ansible installs Flux and hands it the tree on every deploy, so adding, changing or removing an app is an edit there and a deploy, with no Ansible to write. To deploy just the apps:

```bash
make run tags=flux
```

The deploy copies the tree to rackvm1, decrypting vaulted files on the way, pins every `image:` to the digest its tag points at now, and pushes the result to Flux's own Zot, in the `zot` namespace, when it differs from the last push. Flux applies it and waits for everything to become healthy, and the deploy fails if it doesn't. Floating tags therefore move on each deploy, as they do on Swarm. Every 10 minutes Flux re-applies the last push, undoing hand edits to the objects it owns, and an object whose file is deleted is deleted too.

**Hidden apps.** Anything that shouldn't be public goes in a `*.vault.yaml` file, encrypted whole by `make vault-encrypt`, and so does every Secret. File names stay readable, so a hidden suite shares one neutral name (the *arr apps share `media.vault.yaml`), never an app's own. The deploy log is public as well, so the deploy prints no manifests or images, and a failed apply only says to look on rackvm1.

**Namespaces.** Each suite runs in the namespace named by its `[docker_labels]` word, and Portainer, Reloader, k8up, the backup server, ARC and the CI runners in `portainer`, `reloader`, `k8up`, `backup`, `arc` and `ci`. A suite's `kubernetes/<namespace>/namespace.yaml` is a copy of `kubernetes/portainer/namespace.yaml` with the name changed; a hidden suite keeps it in its `*.vault.yaml`. The baseline refuses everything, then lets the namespace's pods reach each other, CoreDNS and the internet, but no private, Tailscale or link-local address, and it refuses host-network pods. Anything more goes in the app's own file: a call between namespaces needs a rule at both ends, naming pods by selector and the port the pod listens on, because a policy sees a Service only after it is translated to a pod; the API server is TCP 6443. No policy keeps a pod's own node out. csi-driver-smb's node plugin is one exception to the baseline: as a storage driver it runs privileged and on the host network, so it stays in k3s's own `kube-system`, which has no Pod Security label. `home` is the other: its app needs the host network, so the namespace enforces `privileged`, and its policies cover only its other pods, such as backup jobs. A host-network pod answers on its node's own address, which a pod selector doesn't match, so a namespace that calls it names the node by `ipBlock` in a vaulted file, and that rule is repointed by hand when the label moves (see Node labels). To check a policy, restart the pods it covers, since open connections outlive a change, and test with TCP, not ping, which always passes, from a test pod at least half a minute old, since the other nodes learn a new pod's address late; `sudo tcpdump -ni nflog:100` on the pod's node names the policy behind each refusal.

**Charts.** An app that ships as a Helm chart is a Flux HelmRelease in its namespace, with the OCIRepository it pulls the chart from beside it, both in the app's file. A chart with no OCI release, like k8up's, comes instead from a HelmRepository, the project's chart website, which has no digest to check; the HelmRelease then names the chart and its `version` range itself. Flux moves the chart to its newest release in the range within the source's interval, CRDs included when the HelmRelease sets `crds: CreateReplace`. Private values go in a Secret in the app's `*.vault.yaml`, read through `valuesFrom` and labelled `reconcile.fluxcd.io/watch: Enabled` so a change to it upgrades the release at once.

**Platform.** Add-ons that apps build on, such as an operator whose resource types the apps use, go in `kubernetes/platform/<name>/` rather than a suite's folder. Flux applies that folder as its own stage, `platform`, and the apps only after it: every deploy applies the platform and waits for it to become healthy, so a new cluster has k8up's types before the first app that uses them. A failure there fails the deploy and holds back the apps until it's fixed.

**Backups.** k8up backs up a namespace's claims with restic into that namespace's own repository on the restic rest-server in `backup`. The server keeps the repositories in `Restic/` on the NAS's Backups drive, which csi-driver-smb mounts as the NAS user `k3s-restic`; that folder is created on first use. Each namespace logs in as itself: a line in the `rest-server-htpasswd` Secret in `kubernetes/backup/rest-server.vault.yaml`, from `htpasswd -nB <namespace>`, which prompts for the password, with the same password in the namespace's own backup Secret. Use a hex password, since k8up puts it into the repository URL unescaped. rest-server picks up a new line without a restart, within a couple of minutes of the deploy, and lets each login reach only its own repository. Any host that mounts the Backups drive can delete the repositories, like the archives beside them. [Volume backup freshness monitoring](#volume-backup-freshness-monitoring) watches each repository's snapshots, and a run that keeps failing shows as a crash-looping container.

A suite backs up with a k8up `Schedule` in `kubernetes/<namespace>/backup.yaml`, a copy of edge's with every `edge` changed to the namespace, the repository's address included, beside a `backup.vault.yaml` holding the Secret `backup`: the login, its own password, and the repository's password, restic's encryption key. Besides this repository, that key lives only in the cluster on rackvm1, so the backups can be read only as long as one of the two survives. restic keeps the password a repository was created with, so changing `repository-password` locks k8up out of it. The Schedule backs up every claim in the namespace daily, prunes weekly to the last 7 daily, 4 weekly and 3 monthly snapshots, and checks the repository weekly, each at a time k8up derives from the namespace, in UTC. Its jobs run as root, so they can read every file whatever its owner, and wait 30 seconds before their first call, since the other nodes refuse a new pod at first. To leave a claim out, annotate it `k8up.io/backup: "false"`. Once a new Schedule is deployed, start its first backup by hand with a one-off k8up `Backup` in the namespace: a prune or check that comes due first creates the repository empty, and **Volume Backup Empty** fires until a backup lands.

`sudo kubectl -n <namespace> get backups,prunes,checks` on rackvm1 lists the last three successful and three failed runs of each, and a label move clears that list. A failing run retries in the same pod up to six times. While it does, `sudo kubectl -n <namespace> get pods -l k8upjob=true` shows the pod, and `sudo kubectl -n <namespace> logs <pod> --previous` says why its last attempt failed. Once the run is marked failed, Kubernetes deletes the pod, and its log with it.

**Restoring.** One command puts a suite's claims back from its backups, on the node it runs on:

```bash
make run tags=k3s-restore ANSIBLE_FLAGS="-e k3s_restore_namespace=<namespace>"
```

It restores every claim in the namespace on its node's disk from its newest snapshot; a claim on a NAS share is restored only when named. Add `-e k3s_restore_claims=<claim>,<claim>` to restore only those, and `-e k3s_restore_before=2026-10-29T12:00` to take each one's newest snapshot from before that time, in UTC, with or without a `Z`; a date alone means its midnight. It:

1. refuses while a label waits to move or another label is left stopped, and while a backup, prune or check is running in the namespace;
2. starts a pod named `restore` in the namespace, with restic from k8up's own image, and picks each claim's snapshot. It names the snapshots it will use and refuses a claim you named that has none; any other claim without one is left as it is. Nothing has changed yet.
3. stops the suite as a move does: pauses Flux and its charts, notes each workload's replicas, deletes its backup Schedules and with them the list of past runs, scales its workloads to zero and stops its pods;
4. starts the `restore` pod again on the suite's node with the claims mounted, and restores each one with `restic restore --delete --verify`, so its folder ends up as the snapshot had it, owners and modes included, with files added since removed, then reads every file back against the snapshot;
5. brings the suite back as a move does, Schedules included, and removes the pod.

If anything fails before the first claim is restored, the suite comes straight back on its untouched data, unless an earlier restore of it was left unfinished, when it stays stopped; running the same command then finishes that one. If a claim fails to restore, the suite stays stopped, since its folders are part-restored, and every deploy refuses to run until the same restore command runs again and finishes. A restore cut off part way, by Ctrl-C or a lost connection, can leave its pod behind with restic still running, and a move of the label refuses until it's gone; running the restore again removes it first. To give up instead and start the suite on whatever its folders hold, run `make run tags=k3s-migrate-labels ANSIBLE_FLAGS="-e docker_migrate_labels=yes"`, which stops a restore left running in it and brings back any label a move or restore left stopped, but also carries out any label move waiting in `[docker_labels]`. Any run with `docker_migrate_labels=yes` does the same, the Swarm move command included. Once the volumes are restored, the apps may write to them, and restoring again would undo that. So if the suite then doesn't come up healthy, the run says so, and a restore cut off after resuming Flux but before putting the workloads back refuses to run again and names the command that finishes it. Cut off later, while it waits for the suite to become healthy, the restore is done, and running it again starts a new one. After rebuilding a node or the cluster, restore before the suite's next backup time, or pass `k3s_restore_before`: a backup of the freshly started, empty apps would otherwise be the newest snapshot.

**A lost node.** When a VM is gone for good, its suites come back on the other nodes from their backups. Keep it off, move its words in `[docker_labels]` to other nodes, and tell k3s it is gone with `sudo k3s kubectl delete node <node>` on rackvm1. Until then a restore of its suites refuses, so a reboot or a network fault is never taken for a loss. Deleting the node takes its pods with it, and a word with no volumes there follows the inventory at the next deploy. Then restore each namespace that had volumes there, with the new node in the run:

```bash
make run hosts=rackvm1,<new node> tags=k3s-restore ANSIBLE_FLAGS="-e k3s_restore_namespace=<namespace>"
```

It checks the new node's data disk is mounted and its folders for those volumes are empty, makes each volume that was on the lost node anew there, restores it from its newest snapshot or its newest before `k3s_restore_before`, puts the label on the new node and brings the suite back. A volume with no snapshot comes back empty, as do all of a suite's volumes if it has no backups, and a volume that was never on the lost node, such as a share on the NAS, is left as it is. Restore `registry` first if it was there, since the other suites pull their images from it. Until a suite is restored it still counts as on the lost node: each deploy reports it waiting to move, Flux may report it unhealthy and fail the deploy, and any run with `docker_migrate_labels=yes` brings back a suite a move or restore left stopped, then refuses to move anything and names the restore. If a restore fails part way, run it again; there is no giving up on it. rackvm1 runs the cluster itself, so this doesn't cover losing it. If the lost VM comes back, it rejoins k3s as a new node with whatever words the inventory still gives it, and its old volume folders stay on its disk until removed by hand. Its NAS driver can lose its registration as the old pods stop; the registrar's probe restarts it, so `sudo k3s kubectl get csinode <node>` lists `smb.csi.k8s.io` again within a minute. If it doesn't, delete the node's `csi-smb-node` pod in `kube-system`.

**Node labels.** Nothing in `kubernetes/` names a node except edge's `allow-home` policy in `traefik.vault.yaml`, which lets Traefik reach `home`'s host-network app at its node's address. When `home` moves, by a move or a lost-node restore, point that `/32` at the new node in the same change, or the app's route fails. Each deploy puts every `[docker_labels]` word on its node as the Kubernetes label `homelab.local/<label>: "true"`, as it does for Swarm, and takes it off any other node. A suite's pods that need their node, for a volume or a port, pin with a `nodeSelector` of that label. A running pod stays where it is when its label moves, until it restarts. A node added or reinstalled with `hosts=` gets its labels from the next run that includes rackvm1, or straight away from `make run tags=flux`.

**Volumes.** An app asks for a volume with a PersistentVolumeClaim of `storageClassName: local-retain`, and a chart is given that class in its values. k3s's local-path provisioner makes the folder, `/mnt/data/volumes/<namespace>/<claim>/data`, on the node the pod is scheduled to, so the pod's node selector decides where it lives. The class keeps the folder when its claim goes, pruned or not, and so does removing k3s from a node; k3s's own `local-path` class would delete it, so the cluster refuses a new claim on any class but `local-retain` unless it names its volume, a chart's claims included. So removing an app leaves its volume Released and its folder in place: once the data isn't wanted, `sudo kubectl delete pv <volume>` on rackvm1 and remove the folder.

**Moving a label.** A label moved in `[docker_labels]` stays on its node, so its k3s apps stay where their data is: each deploy reports it. The Swarm move command (see [Moving a swarm workload](#moving-a-swarm-workload-to-another-host)) moves them first, one label at a time, and the same command with `tags=k3s-migrate-labels` moves only them. A label with nothing on k3s just moves. For any other it:

1. checks that both nodes are in the run and Ready, their data disks and the transit share are mounted, and the new node's folders are empty, unless `docker_migrate_force=yes`, which overwrites them;
2. pauses Flux and the label's charts, notes each workload's replicas on it, deletes its backup Schedules and with them the list of past runs, scales each workload to zero, and deletes the label's volume claims and volumes;
3. copies each of the label's folders on the old node through `docker_migrate_transit_dir` as a tar archive, as the Swarm move does, then compares the two copies' files, owners and modes;
4. moves the label, resumes Flux, which puts the Schedules back, scales the workloads back and upgrades the charts together, so the pods come up on the new node with the copied folders;
5. renames the old folders `data.moved-<time>` for you to remove once you're happy.

A move refuses a label while a backup, prune, check or restore is running in its namespace, and a backup due during the move is skipped until its next time. A failure before step 4 leaves the label where it was, so the apps come back where their data still is. A move cut off part way, by Ctrl-C or a lost connection, leaves the label's workloads scaled down, and every deploy refuses to run until the same move command runs again and finishes it, overwriting its own partial copy. Cut off after the label moved, it also leaves the old folders unrenamed and its `k3s-<label>-<n>.tar` archives in the transit folder, for you to remove. A move onto a node that isn't yet a Ready k3s node stops the run before anything moves, Swarm included; join it first with `make run hosts=<node> tags=k3s`. `edge` moves only with the full command, never `tags=k3s-migrate-labels`, since the k3s Traefik passes what it doesn't route to the Swarm Traefik on its own node; it also needs the UDM's forwards pointed at the new node.

**Moving a suite off Swarm.** Move one suite at a time. Its change takes the suite's services and its `backup_<label>` out of the stack definition, leaving any route of theirs in `external-rules.yaml`, which a failed copy still needs, and adds its app to `kubernetes/`, with its `backup.yaml`, `backup.vault.yaml` and login line (see Backups) so it is backed up from its first day on k3s. For each of its Swarm volumes it adds an entry to `swarm_volume_copies`: the stack's name for the volume, the label, and the app's claim. Set the list in `host_vars/rackvm1/vars.yml` from `vault.yml`, since it names apps. Then:

1. [Pause deploys](#pausing-deploys), merge the change, and wait for any Deploy already running to finish. Until the copy has run, every deploy refuses, `tags=flux` and `tags=docker-stack` included, so an app never starts on an empty folder.
2. From an up-to-date master, run:

   ```bash
   make run ANSIBLE_FLAGS="-e swarm_copy_volumes=yes"
   ```

   For a suite that takes pushes from outside, such as webhooks, run `make run tags=docker-stack,flux ANSIBLE_FLAGS="-e swarm_copy_volumes=yes"` instead. It does only the copy, the stack deploy and Flux, in 3 minutes rather than 15, and GitHub doesn't retry a delivery that fails in between.

3. Start the suite's first backup by hand (see Backups), resume deploys, and in the next change take the suite's entries out of `swarm_volume_copies` and its routes out of `external-rules.yaml`. If the suite had a `backup_<label>`, move its archives out of the monitored root within two days of the copy, whose final backup is their newest, before Volume Backup Stale fires for them (see [Volume backup freshness monitoring](#volume-backup-freshness-monitoring)):

   ```bash
   sudo mkdir -p /mnt/Backups/RetiredVolumes && sudo mv /mnt/Backups/Volumes/<label> /mnt/Backups/RetiredVolumes/
   ```

Ahead of the stack deploy that removes the services, on the label's node, the copy:

1. stops the Swarm services using each volume, noting their replicas on them;
2. has the label's backup service take a final backup;
3. copies each volume with `tar --numeric-owner` into `data.copy` beside `/mnt/data/volumes/<label>/<claim>/data`;
4. compares the two copies' files, owners and modes, then renames the copy to `data`.

The provisioner then gives the app's claim that folder. A failure removes the copies and starts the Swarm services again. A run cut off part way leaves the services stopped and the folder empty, so deploys keep refusing until the same command runs again. A copy refuses while the stack still has services using the volume, or while Swarm and k3s have the label on different nodes, and a label with listed volumes doesn't move.

The Swarm volume is left as it was, as the way back while the label stays on its node:

1. Scale the app to zero, or take it out of `kubernetes/`; `local-retain` keeps its folder.
2. Copy the folder back into the volume if the app has written to it.
3. Restore the service blocks, and any route taken out of `external-rules.yaml`, and deploy.

**Ports on the rackvms.** A port clients reach on a rackvm's own address is a `type: LoadBalancer` Service with `externalTrafficPolicy: Local` and the label `svccontroller.k3s.cattle.io/lbpool: <label>`, its namespace. Each deploy also makes a label's node the ServiceLB pool of that name, and the cluster refuses a LoadBalancer without its namespace's pool, since ServiceLB would otherwise publish it on all five. ServiceLB follows a label that moves, and the Service's address follows its pods. The app sees each LAN client's real address, so its policy admits them by `ipBlock`; a client on the node's Tailscale address or localhost arrives from the Service's ServiceLB pod in `kube-system`, labelled `svccontroller.k3s.cattle.io/svcname` and `svcnamespace`. ServiceLB takes the port on every address of that node ahead of anything listening there, so pick one `ss -lntu` shows free, and remove a Swarm host-mode publish of the same port in the same change. An app that trusts a forward-auth header or has no login of its own is never a LoadBalancer.

**Ingress.** Traefik, `kubernetes/edge/`, answers on rackvm1's ports 80 and 443, redirecting 80 to HTTPS. It keeps its own Let's Encrypt wildcards, for the homelab domain and for `finitelabs.com`, on its volume, solved over Cloudflare DNS with a copy of the Swarm Traefik's key in `traefik.vault.yaml`, so a new key goes in both. An app's route is a built-in `Ingress` with the annotation `traefik.ingress.kubernetes.io/router.entrypoints: websecure`, as the Flux UI's is, and a policy at each end letting Traefik reach the app's port. A request no route of its own claims goes on to the Swarm Traefik on 7443, so it can take the UDM's forwards in place of 7080 and 7443 with every Swarm host still served. Those requests reach the Swarm Traefik from docker_gwbridge's gateway, 172.19.0.1, which it trusts to report the client's address. Beside it, `homebridge-cert` pushes the certificate to the Homebridge UI on rackmini1 every hour, as the input to the forced command `homebridge-tls-install`, which restarts Homebridge only when the certificate has changed.

**Sign-in.** A route behind the house sign-in, Keycloak's homelab realm, names `strip-auth-request@file,oauth-verify@file` in its Ingress's `traefik.ingress.kubernetes.io/router.middlewares` annotation. Both are defined in the file provider in `traefik.yaml` and named as on Swarm. `oauth-verify` asks oauth2-proxy in `edge` (`oauth2-proxy.yaml`) about each request: a signed-out browser goes to Keycloak and comes back to the page it asked for, and a signed-in one reaches the app with `X-Auth-Request-Email`, `-User`, `-Preferred-Username` and `-Groups` naming the user, after any `X-Auth-Request-*` header the client sent is dropped. `strip-auth-request` blanks those headers too, and on its own serves a route that skips sign-in but reaches an app that reads them. Keycloak sends the browser back to `/oauth2/callback` on the app's own host, so each guarded host also needs:

- a rule for its `/oauth2/` paths in the Ingress `oauth2-proxy` in `oauth2-proxy.vault.yaml`, created with the first one;
- `https://<host>/oauth2/callback` in the Valid redirect URIs of the realm's `oauth2-proxy` client in Keycloak. Until then, sign-in stops at Keycloak's "Invalid parameter: redirect_uri".

oauth2-proxy uses the Swarm one's client and cookie secret, from the same vault values, so a new client secret goes in both, and a browser signed in to a host on Swarm stays signed in when that host moves to k3s. A guarded app, like any app that trusts these headers, is reached only through Traefik: its policy admits only edge's Traefik.

**Finite Labs sign-in.** Apps at `*.finitelabs.com` sign in against Keycloak's `finitelabs` realm instead, through a second oauth2-proxy, `oauth2-proxy-finitelabs`, beside the first. Such a route names `strip-auth-request@file,oauth-verify-finitelabs@file`, its `/oauth2/` rule goes in the Ingress `oauth2-proxy-finitelabs`, and its callback in the `finitelabs` realm's `oauth2-proxy` client, a copy of the homelab realm's. That proxy has its own client and cookie secrets, in `oauth2-proxy.vault.yaml`, and requires the realm's default role, `default-roles-finitelabs.com`. The names are set up in the Finite Labs Cloudflare account, which is not the homelab's:

- `*.finitelabs.com` is a DNS-only CNAME to the same home address the homelab names use.
- Traefik's key can't write that zone, so `_acme-challenge.finitelabs.com` is a CNAME to `_acme-challenge.finitelabs` in the homelab zone, where Traefik writes its challenge. A placeholder TXT at that homelab name keeps the homelab wildcard from answering for it; deleting it breaks the `finitelabs.com` certificate's renewal.

**Sign-in addresses.** Each realm's pages are served at its own `auth.` name, set as the realm's Frontend URL in Keycloak: `auth.<homelab domain>` for the homelab realm and `auth.finitelabs.com` for `finitelabs`. The Swarm Traefik routes both to Keycloak. Keycloak's own name stays for the admin console, whose password sign-in is the way back in when a realm's passkeys fail. A realm's issuer is its `auth.` address. For `finitelabs` that's named only by `oauth2-proxy-finitelabs`; for the homelab realm, by the Swarm oauth2-proxy, the k3s one in `oauth2-proxy.vault.yaml`, the Flux UI's `flux_web_issuer_url`, Proxmox's `proxmox_oidc_issuer_url` and YouTrack's Keycloak auth module. Changing a Frontend URL changes the issuer at once, and an oauth2-proxy whose issuer doesn't match refuses to start, so change the URL first and deploy the rest straight after. Each realm's WebAuthn Passwordless Policy sets the Relying party ID to its whole domain, so a passkey works on any of its subdomains; a passkey made under another ID is never offered. To register a new one, bind the stock `browser` flow, which takes a password, sign in at `https://auth.<domain>/realms/<realm>/account`, set up a passkey under Signing in, then bind the realm's passkey flow (`browser-passkeys` or `browser-passkey`) back.

**Sign-in pages.** Each realm has its own look: the homelab's is `rack`, the `finitelabs` realm's follows finitelabs.com, and both switch with the device's light or dark setting. oauth2-proxy's error pages are the ConfigMaps in `oauth2-proxy-pages.yaml`, and Reloader restarts a proxy when its ConfigMap changes; a template that doesn't parse stops that proxy from starting. Keycloak's login themes, `rack` and `finitelabs`, are files under `playbooks/files/plaintext/rackvm1/var/docker/etc/keycloak/themes/`, mounted into the Swarm Keycloak on the `identity` node. Each only restyles Keycloak's `keycloak.v2` theme, so passkey, OTP and "Try another way" behave as stock. A realm takes one under Realm settings, Themes, Login theme. Keycloak caches themes, so after editing one, restart it with `docker service update --force rackvms_keycloak` on a manager.

**Image registry.** Every deploy pushes the images the build hosts make (`generic/docker/build-images`) to the image registry, a second Zot in `kubernetes/registry/` on the `registry` label's node, as `<name>:<input hash>` and `<name>:latest`. It keeps each image's three most recently pushed tags, and an older build for a month after it was last pushed or pulled. An app names one `registry.homelab.local/<name>`, with no tag, which each node's `/etc/rancher/k3s/registries.yaml` maps to the registry with a read-only account. The deploy pins it to `registry.homelab.local/<name>@<digest>`, which a node runs from its own copy once it has one; with a `:latest` tag Kubernetes would pull on every start. While the registry doesn't answer, as on a new cluster before this deploy has created it, its images keep their tag and the deploy says so, rather than failing. The registry answers only clients on the home network, every pod and container included, since they all reach it through the UDM; its accounts are what keep the rest out. The build hosts push as `builder` and the nodes pull as `k3s`: both passwords are in `host_vars/rackvm1/vars.yml` and their bcrypt lines in `zot.vault.yaml`, so a new password changes both in one commit, and pushes or pulls with it are refused until that deploy has applied the new line. Vault the password with `printf '%s'`, since a trailing newline makes it a different password. On a new cluster the first deploy's pushes fail, since the registry comes up after the builds, and the next deploy makes them.

**Status.** On rackvm1:

```bash
sudo KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux get all -A
```

lists the sources and the two Kustomizations, `platform` and `apps`, with the revision each last applied, or the error. The Flux Operator's web UI shows the same at `https://flux.<domain>`, served by the k3s Traefik. It signs in through Keycloak with oauth2-proxy's client, so `flux_web_client_secret` in `host_vars/rackvm1/vars.yml` changes whenever that client's secret does; `flux_web_admin` may run its actions, and anyone else signed in sees only the dashboard. Portainer's `rack-k3s` environment shows Flux's apps as external; change them in the repo, since Flux reverts edits made there.

**Metrics.** Each node's Telegraf reads its own kubelet as the `telegraf` ServiceAccount in `kube-system` (`generic/k3s-stats`), writing `kubernetes_pod_container` (usage) and `kubernetes_pod_container_status` (state, restarts, readiness) among others. On rackvm1 it also lists the nodes, Deployments, StatefulSets and DaemonSets through the API, writing `kubernetes_node_status` and `kubernetes_<kind>_status`, which Grafana's **Node Not Ready (k3s)**, **Workload Unavailable (k3s)** and **k3s API Unreachable** rules read: a node not Ready for 5 minutes, a workload short of ready replicas for 10, and nothing from the API for 3. The role gives it only `get` on `nodes/stats` and `nodes/pods` and `list` on those four kinds; never add `nodes/proxy`, which also allows exec into any container on the node. A new grant takes the kubelet about 30 seconds to honour. To replace the token, delete Secret `telegraf-token` in `kube-system`, run `sudo kubectl apply -f /mnt/data/k3s/server/manifests/telegraf-rbac.yaml` on rackvm1, then `make run tags=k3s-stats`.

**Secrets.** Reloader restarts a workload whenever a Secret or ConfigMap it uses changes, so a rotated value takes effect with the deploy that ships it.

**Portainer.** The cluster's Edge agent is `kubernetes/portainer/`. To re-key it, add the environment again in Portainer (Kubernetes, Edge Agent Standard) and take the Edge ID and key from the command it shows, without running it. Put them in `kubernetes/portainer/edge.vault.yaml`.

**Upgrading.** The Flux Operator chart and Flux's Zot chart pin only their major in `generic/flux/defaults/main.yml`, and each deploy moves them to the newest release in it, waiting until k3s has rolled each one out. Flux follows `2.x`: each operator release brings the newest Flux it ships, and the deploy installs the flux CLI at that version. To hold a chart back, set it to a minor such as `0.61.x` or an exact version such as `0.61.0`; crossing a major means changing the value. An older operator can only take over while it ships the Flux minor the cluster already runs, so roll a bad operator forward rather than back once it has moved Flux up a minor.

**Flux's registry password.** Change `flux_registry_password` and `flux_registry_htpasswd` in `host_vars/rackvm1/vars.yml` together, each as `ansible-vault encrypt_string` output. `htpasswd -nB flux` prompts for the new password and prints the line for the second. Flux's Zot keeps its volume at `/mnt/data/volumes/zot` on rackvm1, which holds only what deploys pushed. If it is lost, apps keep running and Flux reports its source failing until the next deploy pushes again.

**Removing it.** This deletes every app Flux applied, Reloader among them, then Flux and its Zot with its volume. Run it before removing k3s from the server:

```bash
make run hosts=rackvm1 tags=flux ANSIBLE_FLAGS="-e flux_state=absent"
```

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

Before moving to the next node it also waits, up to `proxmox_reboot_k3s_timeout` (default 300s), for the k3s server guest (`proxmox_reboot_k3s_server`, rackvm1) to answer `/readyz` with ok. The API answers that only to an authenticated client, so the check runs inside the guest through the QEMU guest agent on whichever node runs it. A timeout stops the rolling loop like a guest that doesn't come back.

`proxmox_reboot_ha_mode` (default `downtime`) governs how the node's HA resources are protected during the reboot itself, not whether guests come back after:
- `downtime` — marks them `--state stopped` so HA doesn't fence them while the node is down; they resume in place once it's back.
- `migrate` — puts the node into HA maintenance first, which live-migrates its HA resources off before rebooting. Only wins if live migration is actually fast; two offline moves (off, then back) is usually slower than the reboot it was meant to avoid, which is why `downtime` is the default.

Non-HA guests are never started by this play — it only *waits* for guests that were running before the reboot to be running again, which only happens for guests with `onboot: yes`. That wait is not a soft check: it has no `ignore_errors`, so a guest that doesn't come back within `proxmox_reboot_guest_timeout` (default 300s) fails the task hard, loudly, in the output. That failure sits outside the per-node `block`/`always`, so it stops the whole rolling loop — every node still left in `proxmox_reboot_order` is skipped, even though the failed node's own HA handback already ran cleanly before the failure. Not reachable on the current fleet (every running non-HA guest on pve1-5 has `onboot: 1` today), but a new guest added without it will stall a rolling reboot partway through rather than being skipped over.

A final play reapplies `generic/proxmox` (`scope: node`) across the whole cluster whenever `proxmox_upgrade_enabled` was set, whether or not any node actually rebooted — a PVE upgrade can reset node-level settings the role manages, and reasserting them unconditionally afterward is simpler than detecting what an upgrade actually touched. That's true of the clean `meta: end_play` exit above, but not of a failure: the rolling loop runs as a single `localhost` play with no error-control override, so the hard failure described above aborts the whole playbook run on the spot — the reapply play never starts, not even for nodes that already upgraded before the failure.

## Volume backup freshness monitoring

`offen/docker-volume-backup` emits no metrics, and its `NOTIFICATION_URLS` only fire on a run that actually happens — a container whose cron has stopped is silent and looks healthy. So the monitoring watches the *outcome* on the share rather than the process: `generic/volume-backup-stats` runs a Telegraf `filecount` input over `/mnt/Backups/Volumes/*` and reports `count` and `newest_file_timestamp` per target directory.

The glob is load bearing. `filecount` emits **one series per entry in `directories`**, and `recursive` only controls how deep it walks to build that one aggregate — so pointing it at the root with `recursive = true` yields a single series whose `newest_file_timestamp` is whichever target ran most recently. Every stalled target hides behind a healthy one. Globbing the children instead gives one series per target and is layout agnostic, so a new backup service needs no monitoring change.

Every backup container, on the swarm and on the Pis, also posts failed runs to the Homelab Backups Discord webhook through `NOTIFICATION_URLS` (`docker_volume_backup_notification_urls`, vaulted in `group_vars/all`). That covers a run that starts and fails, including a copy to the share that breaks off partway; the rules below cover a run that never starts.

It runs on one host (`[volume-backup-stats]`, currently `rackvm4`), since every rackvm mounts the same share and five copies of the same numbers buy nothing.

A second `filecount` input counts the snapshots in each k3s namespace's restic repository, the files in `<k3s_backup_root>/*/snapshots`: k8up writes one for each claim every time it backs up. Each repository is one more `volume_backups` series, so the rules below cover it unchanged. k8up retries a failed backup, prune or check in the same pod, up to six times, so a run that keeps failing shows as **Container Crash Loop (k3s)**, for `<namespace>/backup`, `/prune` or `/check`; one that fails once and then succeeds raises nothing.

Anything placed under `docker_volume_backup_root`, and any repository under `k3s_backup_root`, is monitored, unconditionally. There is no exclusion list and no name matching, which is what keeps a new backup target from needing a monitoring change to be covered — and what stops a rename from silently dropping one out of coverage.

That is why retired backups go in `/mnt/Backups/RetiredVolumes/`, a sibling of `Volumes` rather than a child, and a retired namespace's restic repository goes in `/mnt/Backups/RetiredRestic/`. A folder of deliberately kept archives inside the monitored root would sit at `count > 0` with an old timestamp forever, which is a `Volume Backup Stale` alert that can never clear. Restoring from a retired archive needs `docker_restore_volumes_backup_dir` set directly: `generic/docker/restore-volumes` derives its path as `<docker_volume_backup_root>/<label>`, so `docker_restore_volumes_label` cannot reach outside the monitored root. rest-server sees only `Restic/`, so a retired repository is restored by moving it back there, with its namespace's login and backup Secret in place; it then reads as stale until its next backup.

A **decommissioned host** is the mirror case and takes the opposite handling: its target directory is left empty with nothing worth preserving, so it belongs in neither place and wants removing outright. Use `rmdir` rather than `rm -rf` — the check and the delete are then the same syscall, so a directory that still holds archives refuses to be removed instead of being destroyed, and that refusal is the signal that the case was a retirement after all.

Skipping either cleanup fails quietly in the same way: kept archives inside the monitored root are a permanent `Volume Backup Stale`, an emptied target is a permanent `Volume Backup Empty`, and both alerts are correct — there is nothing to fix but the directory, which is what turns them into background noise. Time the removal against the collector rather than against the alert. A target removed while its samples still read healthy never reports a problem at all; one left in place until it empties out or goes stale pages first, and then goes on reading that way until its last sample ages out of the rule's 15 minute window. The same ordering applies to a directory the collector has not scraped yet, which is why a dead target found during a monitoring rollout should be cleared before the rules are unpaused rather than after.

Three alert rules, in the Grafana rule group `backup`, because one threshold does not cover the failure modes. This repo provisions no Grafana alerting, so they are configured in Grafana itself and appear nowhere in this tree:

- **Volume Backup Stale** — `newest_file_timestamp` older than 2 days, filtered to `count > 0`. Retention is 7 daily archives, so 2 days is clear of a single missed run. The `count > 0` filter matters: `filecount` reports `newest_file_timestamp = 0` for a directory with no matching files, which reads as epoch 1970 and would otherwise pin that target to a permanent stale alert.
- **Volume Backup Empty** — `count == 0`, the case the staleness rule excludes. Catches a target that has never produced an archive, or whose whole retention window has aged out.
- **Volume Backup Metrics Absent** — no series at all. An unmounted share emits *nothing* rather than something stale, so silence and health look identical to the first two rules. `Host Deadman` already covers rackvm4 being down, but not Telegraf running fine on a host whose CIFS mount has dropped.

What none of this catches: a backup target directory being **deleted** disappears from the glob silently, the same shape as the absent case but scoped to one target. A pruned service leaves its directory behind, so the ordinary stop-producing case is covered; only an actual `rm -rf` of the target is not. On k3s, a run that hangs until its hour is up, or whose pod never starts, restarts nothing: such a backup shows only once its repository goes stale, and such a prune or check only in `sudo k3s kubectl -n <namespace> get prunes,checks`. A repository's freshness is its newest snapshot of any claim, so in a namespace with several claims, one that stops being backed up while the others carry on is hidden too.

Nor does it catch a *partial* archive. `offen` builds the archive under `/tmp` inside the container and then byte-copies it onto the share under its final name, and it does not unlink the destination if that copy dies partway (`internal/storage/local/local.go`: `os.Create` then `io.Copy`, no rename anywhere in the tree). A copy interrupted by a dropped mount leaves a truncated `backup-<ts>.tar.gz` carrying a current mtime, which reads as fresh. Freshness here means an archive arrived, not that it is complete or restorable.

YouTrack writes its own scheduled backups into the `youtrack-backups` volume (Database Backup settings: location `/opt/youtrack/backups`, TAR.GZ, keep 2), and `backup_youtrack` archives that volume with the rest. Telegraf cannot read inside node-local volumes, so `youtrack-backup-check` watches it instead: it exits after 60 s whenever no `*.tar.gz` there is newer than 26 h, and a stopped schedule therefore shows as **Container Crash Loop (Swarm)** for that service.

## Bringing up a new host

1. Add the host to `playbooks/hosts` under its groups (a swarm node also under `[docker_labels]`) and give it `playbooks/host_vars/<host>/`.
2. **First contact goes over the LAN.** Tailscale hosts are reached at `<host>.ts.<domain>`, which exists only once the host's first Tailscale run has published it. Set the new host's `ansible_host` to its LAN address in its host_vars, run `make bootstrap hosts=<host> user=<user>` and then `make run`, and switch `ansible_host` to the tailnet name afterwards. Don't pass `-e ansible_host=<address>` instead: an extra var applies to every host, so `influxdb3_url`, built from the metrics host's `ansible_host`, would point the new host's Telegraf at itself.
3. **A swarm node needs a manager in the run.** The swarm play joins it, then gives it its `[docker_labels]`; a label already on another node is held until it moves with its volumes (see [Moving a swarm workload](#moving-a-swarm-workload-to-another-host)). Use a full `make run`, or include another manager in `hosts=`.

When a host is rebuilt under the same name:

- **Swarm node:** its old entry stays in `docker node ls` as Down, and the label reconcile needs node names to be unique, so remove it first. Its labels go with it, and a label landing on the rebuilt node starts that label's services on empty volumes, which a later restore then skips as already holding data. So bring the data back before the labels:
  1. On a manager: `docker node demote <old-id>` if it was a manager, then `docker node rm --force <old-id>`.
  2. Clear the host's `docker_labels` in `[docker_labels]` and run, with another manager in `hosts=`. The node joins with no labels. If it carries `metrics`, add `ANSIBLE_FLAGS="-e influxdb3_metrics_host=<host>"`: with no host holding that label, Telegraf's output has nowhere to point and the play fails.
  3. Restore each label's volumes onto it, as in [Restoring volumes from a backup archive](#restoring-volumes-from-a-backup-archive), with `-e docker_restore_volumes_label=<label>`.
  4. Put the labels back and run again; the services start on the restored volumes.
- **Tailscale:** remove the old, offline machine in the admin console, and approve any routes the host advertises again.
- **SSH:** if it comes back at an address used before, run `ssh-keygen -R <address>` before `make bootstrap`. With a changed key SSH refuses the password login the bootstrap uses.

## macOS hosts: manual steps at bootstrap

Some macOS state cannot be set from an Ansible run, and the reason differs per case. The playbook does not handle these uniformly, so read the bullet rather than assuming a failed run told you about the problem: `generic/smb-mount` halts on an interactive `pause:` with a 10-minute timeout, `generic/plex` only prints a `debug:` note and continues green, and FileVault has no detection at all.

- **SMB shares** — the `local.smb-mount` LaunchDaemon mounts each share as `mac_user`, remounts it within 30 s of it going missing, and logs to that account's `~/Library/Logs/local.smb-mount.log`. A run fails when a share isn't mounted by `mac_user` a minute after the daemon starts, and the log says why. Apple's own binaries (`/bin/sh`, `cp`, ...) started by launchd cannot read the shares, so a launchd job that reads them must run a third-party binary such as node.
- **Syslog forward** — macOS updates drop the forward to Telegraf from `/etc/syslog.conf`, so after every OS update re-run `make run hosts=<host> tags=telegraf`.
- **`/mnt` synthetic link** — macOS's sealed system volume can't hold a root-level `/mnt`, so the mount role declares it in `/etc/synthetic.conf`, which only takes effect at boot. On a Mac's first run the role pauses for 10 minutes and asks for `sudo /sbin/shutdown -r now`. Reboot, log in at the Mac (FileVault keeps SSH down until someone does), then press Ctrl+C and C to resume sooner. If the pause runs out first, re-run with `tags=smb-mount`.
- **sudo password** — a Mac admin's `sudo` asks for a password, so bootstrap a Mac with `make bootstrap hosts=<host> user=<admin> become_pass=1`.
- **Homebrew** — before `make bootstrap`, install Homebrew into `/opt/homebrew` as the host's `mac_user`. Every `brew` command runs as `mac_user`, so Homebrew must belong to that account.
- **Remote Login access list** — set System Settings > General > Sharing > Remote Login to "Only these users" before `make bootstrap`. The bootstrap adds the `ansible` account to that list, and while Remote Login admits all users the list's group doesn't exist, so the bootstrap stops at `Group com.apple.access_ssh does not exist`.
- **Plex "Open at Login"** — the menu-bar app registers its own LaunchAgent via LSSharedFileList, which needs Automation TCC and so cannot be set programmatically. After `generic/plex` installs the cask, launch Plex once via VNC or console and tick the box. The role prints instructions until the LaunchAgent appears.
- **Homebridge config** — `config.json` (plugins, HomeKit pairings, its TLS paths, `bridge.bind`) is application state, not repo state, so a replacement Mac needs its Homebridge backup restored.
- **OpenClaw** — install it with npm and onboard it with the gateway on the LAN at port 18789, then run `openclaw gateway install`, or restore `~/.openclaw` from a backup. Until then a run fails at the `openclaw` play, which sets only the `hooks` block in `openclaw.json` (`openclaw_hooks`, the block the webhook proxy posts to); the rest of that file is application state. The hooks token and timeout are each in two places, `vault_openclaw_hooks_token` and `openclaw_hooks_timeout_seconds` in rackmini1's host_vars, and `OPENCLAW_HOOKS_TOKEN` and `OPENCLAW_TIMEOUT_SECONDS` in `kubernetes/webhooks/openclaw-webhook-proxy.vault.yaml`, so change both. A deploy moves the proxy to them first and OpenClaw about two minutes later, `make run tags=flux,openclaw` within seconds; a turn sent in between fails and is retried five minutes later.
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

The `rack` runner set in `kubernetes/ci/rack.yaml` runs the catalogue job of `finitelabs/control4-zigbee3`, which asks for `runs-on: rack`, through GitHub's Actions Runner Controller (ARC), whose controller is in `kubernetes/platform/arc/`. ARC's listener, a pod in `arc`, waits for jobs. Each job gets a fresh runner pod in `ci`, registered with a one-time configuration and deleted when the job ends, so no job inherits another's workspace and the token never reaches a pod that runs a job. Up to four run at once, one per node and never on the k3s server; with no job queued there are none.

**Sizing.** Each runner is limited to 4 CPUs and 8 GiB, half of an 8-core rackvm. Node's `os.availableParallelism()` reads the CPU limit, so a job that sizes its workers by core count starts 4 of them, and its memory follows; the catalogue peaks near 7 GiB. Each asks for only 1 CPU and 2 GiB, so the scheduler fits it beside a node's apps.

**Chart versions.** The controller's chart and the runner set's are both held to the same minor, `0.15.x`, because a controller deletes a runner set made by a chart of another minor, cancelling its jobs. Move both in one change, while no job runs.

**The token.** The controller and listener register runners with a fine-grained personal access token, `github_token` in the Secret `rack-github` in `kubernetes/ci/rack.vault.yaml`. To set or rotate it:

1. On GitHub, Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token.
2. Resource owner `finitelabs`. Repository access: Only select repositories, `finitelabs/control4-zigbee3`.
3. Repository permissions: Administration, Read and write. GitHub adds Metadata, Read-only; grant nothing else.
4. Expiration: the longest the organization allows. A lapsed token stops new registrations (below), so note the date.
5. If `finitelabs` requires approval for fine-grained tokens, approve the request under the organization's Settings > Personal access tokens > Pending requests.
6. Copy the token. With the vault decrypted (`make vault-decrypt`), put it in `kubernetes/ci/rack.vault.yaml` as `github_token`, then run `make vault-encrypt` and commit.
7. Merging deploys it, but ARC doesn't watch the Secret, so restart the listener on rackvm1 with `sudo kubectl -n arc delete pod -l app.kubernetes.io/component=runner-scale-set-listener`; ARC starts a new one with the new token, and running jobs carry on.
8. Once a job has run on the new token, delete the old one under Fine-grained tokens.

**A bad or expired token** shows in the listener's and the controller's logs in `arc`, and jobs then wait on GitHub with no runner. No alert covers it yet.

**Updating the runner.** Nothing to bump. The image `gha-runner` builds on rackvm2 from `actions-runner:latest`, tagged by a hash of its inputs, the base image's digest included, so each deploy builds a new runner release by itself, pushes it to the registry and pins the runner set to it. ARC replaces idle runners with the new one and lets busy ones finish. ARC turns the runner's own self-update off, so a runner is as new as the last deploy's build. The weekly deploy keeps that within a week of GitHub's. If GitHub refuses a runner release it has retired first, ARC parks the runner set, which `sudo kubectl -n ci get ephemeralrunnerset` shows as `Outdated`, while jobs queue; running the Deploy workflow builds a newer runner and starts the set again.

**Removing it.** Take out `kubernetes/ci/` and deploy before removing the controller: the runner set's resources carry finalizers only the controller clears, and with both gone at once they hang and leave the runner set registered on GitHub.

## Zigbee firmware index

`kubernetes/firmware/` runs the Zigbee firmware index ([finitelabs/zigbee-firmware-index](https://github.com/finitelabs/zigbee-firmware-index)) on the `firmware` label's node. One pod serves the admin page at `https://ota-admin.finitelabs.com`, behind the Finite Labs sign-in (see [Apps on k3s](#apps-on-k3s)), and publishes the index to the R2 bucket `zigbee-firmware-index`, which Cloudflare serves at `https://ota.finitelabs.com`. Anyone in the `finitelabs` realm can sign in, but only the addresses in the `admin` Secret may use the page. The pod keeps its SQLite database and uploads on the claim `data`; the published files live only in R2.

**Runs.** The app publishes once a day at the UTC time set on its Runs page, 04:00 unless changed, and catches up a run missed while it was down; the setting lives in its database, not in the repo. A failed run, a run the 5% brake held back, and a vendor source failing or recovering post to Discord through the app's own webhook, in the `alerts` Secret. A check in the app's own repository fetches `https://ota.finitelabs.com/latest.json` every 6 hours from outside and posts to Discord if it is over 48 hours old or its signature fails. **Workload Unavailable (k3s)** and **Container Crash Loop (k3s)** cover the pod, which refuses to start with any setting missing.

**The image** is `ghcr.io/finitelabs/zigbee-firmware-index:1`, public, and each deploy pins it to that tag's digest, so a release within the major arrives with the next deploy. A new major means changing the tag.

**The signing key** in the `signing-key` Secret signs every index the app publishes, and drivers in the field check it against the public key they ship with. Besides `firmware.vault.yaml`, keep a copy in the password manager. Changing it takes a driver release first: ship a driver that accepts both keys, switch the Secret once that driver is out, and drop the old key in a later driver. If it leaks, switch the daily run off on the Runs page until the two-key driver ships.

**Backups.** `backup.yaml` backs the suite up daily like any other (see Backups under [Apps on k3s](#apps-on-k3s)). Besides the claim's files, k8up runs `python -m firmware_index dump` in the pod, as the Deployment's `k8up.io/backupcommand` annotation asks, and saves its consistent copy of the database as a snapshot of its own, at the path `/firmware-firmware.sqlite3`. It reaches the pod through the API server, which `allow-kube-apiserver` lets the namespace's k8up jobs do. The claim's own copy of the database can be caught mid-write, so if one restored with `k3s-restore` won't open, put the dump in its place:

1. `sudo kubectl -n firmware scale deploy/firmware --replicas=0` on rackvm1.
2. Start a pod in `firmware` labelled `k8upjob: "true"`, from the image a backup job uses, with the `backup` Secret's `username`, `password` and `repository-password` in its environment, and in it run `restic -r rest:http://<username>:<password>@rest-server.backup.svc.cluster.local:8000/firmware/ dump --path /firmware-firmware.sqlite3 latest /firmware-firmware.sqlite3`, with the output going to a file. Without `--path`, `latest` is the claim's snapshot, which has no such file.
3. On the `firmware` node, put that file at `/mnt/data/volumes/firmware/data/data/index.sqlite3`, owned by 10001, and delete `index.sqlite3-wal` and `index.sqlite3-shm` beside it.
4. Scale the Deployment back to 1.

**Cloudflare, once, by hand.** In the Finite Labs Cloudflare account, which holds `finitelabs.com` and is not the homelab's:

1. R2 Object Storage: add the R2 subscription if the account has none, then Create bucket `zigbee-firmware-index`, location Automatic, Standard storage class.
2. The bucket's Settings > Custom Domains > Add: `ota.finitelabs.com`. Leave its Public Development URL off.
3. R2 Object Storage > API Tokens > Create Account API token: Object Read & Write, Apply to specific buckets only, `zigbee-firmware-index`, TTL Forever. It shows the Access Key ID and Secret Access Key once; they go in the `r2` Secret as `access-key-id` and `secret-access-key`, beside `bucket` and `endpoint`, `https://<account id>.r2.cloudflarestorage.com`, the account ID being the one in the dashboard's URL.
4. The two DNS records under [Apps on k3s](#apps-on-k3s), Finite Labs sign-in.
5. The zone is on Pro, with Super Bot Fight Mode set to Managed Challenge for "Definitely automated", which would also challenge drivers and the outside check fetching the index. Under Security > WAF > Custom rules, a rule placed first, "Firmware host: no browser checks", matches Hostname equals `ota.finitelabs.com` and skips all remaining custom rules, all Super Bot Fight Mode rules, Browser Integrity Check and Security Level, still logging what it matches. Without it, the outside check gets a challenge page and a plain HTTP client gets error 1010.

To try a release against something other than the real bucket, `zigbee-firmware-index-test` is kept empty for it. Make a token scoped to it as in step 3, and point `r2` at both; the first start publishes. Before pointing the app back, remove the claim's folder: the database remembers what it uploaded, and would skip those files in the real bucket.

**Keycloak.** The admin page's callback, `https://ota-admin.finitelabs.com/oauth2/callback`, is in the `finitelabs` realm's `oauth2-proxy` client. The `admin` Secret lists realm users by their email, which is what the sign-in passes to the app.

**The outside check.** Once `https://ota.finitelabs.com/latest.json` serves, set the app repository's secret `DISCORD_WEBHOOK_URL` to the app's webhook, its variable `SIGNING_KEY_PUB` to the public key as hex, as `https://ota.finitelabs.com/signing-key.pub` serves it, and its variable `OTA_CHECK_ENABLED`, which turns the check on. GitHub switches a schedule off after 60 days with no activity in the repository; its Actions page then shows the workflow disabled.
