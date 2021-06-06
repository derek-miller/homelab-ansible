Installing requirements into active virtualenv
==============================================

To install requirements after cloning/pulling the repo:

    make after-git-pull

Compiling requirements
======================

To (re-)compile `requirements.txt`:

    make -B requirements.txt

To actually upgrade the newest versions of unpinned packages:

    make -B requirements.txt UPGRADE=1

Running locally
===============

    make bootstrap
    make run env=<env>
    make run env=<env> host=<host>

Setting up Ansible Vault
------------------------

From the project directory:
1. `mkdir -p .vault_pass && cd .vault_pass`
2. `touch <env>`
3. `chmod 0600 *`
4. Copy the vault passwords into those files with e.g.
   `pbpaste > <env>`

Passing Ansible flags
---------------------

To provision and pass ansible flags:

    make run ansible_flags='--skip-tags=common,base --tags=ethereum'
    make run hosts=miner1  # shorthand for passing the --limit flag

Bootstrapping
-------------

You'll need to bootstrap any new hosts:

    make bootstrap env=<env> hosts=<new-host> user=<your-local-user>

Adding local SSH known hosts entries for inventory hosts
--------------------------------------------------------

To get ssh hostname completion and known host keys for hosts in an inventory:

    make known-hosts
    make known-hosts env=<env> hosts=<host>


Exporting Grafana Dashboards
----------------------------

To export grafana dashboards:

    make export-dashboards env=<env>

(Re)installing Ansible Galaxy roles
-----------------------------------

To (re-)install Ansible Galaxy roles:

    make galaxy-install
