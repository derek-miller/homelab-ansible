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

    make bootstrap hosts=<new-host> user=<your-local-user>
    make run
    make run hosts=<host>

Setting up Ansible Vault
------------------------

From the project directory:
1. `touch .vault_pass`
2. `chmod 0600 .vault_pass`
3. Copy the vault passwords into those files with e.g.
   `pbpaste > .vault_pass`

Passing Ansible flags
---------------------

To provision and pass ansible flags:

    make run ansible_flags='--skip-tags=common,base --tags=vnc'
    make run tags=vnc  # shorthand for the above command
    make run hosts=raspi1  # shorthand for passing the --limit flag

(Re)installing Ansible Galaxy roles
-----------------------------------

To (re-)install Ansible Galaxy roles:

    make galaxy-install
