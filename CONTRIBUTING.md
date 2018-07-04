Installing requirements into active virtualenv
==============================================

    make init       # installs/upgrades setuptools, pip, pip-tools, etc.
    make install    # installs/upgrades project requirements

Compiling requirements
======================

To (re-)compile `requirements.txt`:

    make -B requirements.txt

To actually upgrade the newest versions of unpinned packages:

    make -B requirements.txt UPGRADE=1

Running locally
===============

    make bootstrap
    make run

Setting up Ansible Vault
------------------------

From the project directory:
2. `touch .vault_pass`
3. `chmod 0600 .vault_pass`
4. Copy the vault password into your clipboard and paste it into the `.vault_pass` file with e.g. `pbpaste > .vault_pass`

Passing Ansible flags
---------------------

To provision and pass ansible flags:

    make run ansible_flags='--skip-tags=common,base --tags=ethereum'
    make run hosts=miner1  # shorthand for passing the --limit flag

Bootstrapping
-------------

You'll need to bootstrap any new hosts:

    make bootstrap

Adding local SSH known hosts entries for inventory hosts
--------------------------------------------------------

To get ssh hostname completion and known host keys for hosts in an inventory:

    make known-hosts
    make known-hosts hosts='miner1'

Creating a virtualenv with pyenv
================================

To create a virtualenv for this project:

    pyenv virtualenv $(pyenv global) ansible-mining

To automatically activate the virtualenv when you navigate to this directory:

    pyenv local ansible-mining

To install pyenv in the first place:

### OS X

    brew install pyenv pyenv-virtualenv
