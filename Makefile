PIP_INDEX_URL = https://pypi.python.org/simple

ifeq ($(VIRTUAL_ENV),)
$(error must run in a virtualenv)
else
$(info running in virtualenv $(VIRTUAL_ENV))
endif

playbook ?= $(or $(PLAYBOOK),$(DEPLOY_PLAYBOOK),mining)

inventory_file = hosts
playbook_file = mining.yml
vault_password_file = .vault_pass

vault_files = $(shell find files host_vars group_vars -type f -path '*vault*')

ansible_default_flags = --inventory=$(inventory_file) \
						$(if $(vault_password_file),--vault-password-file=$(vault_password_file))

ansible_flags ?= $(or $(ANSIBLE_FLAGS),$(ANSIBLE_OPTS))
override ansible_flags += $(if $(hosts),--limit='$(hosts)')

ansible_playbook_cmd = ansible-playbook $(playbook_file) $(ansible_default_flags) $(ansible_flags)
ansible_setup = ansible -m setup $(ansible_default_flags) $(ansible_flags)

ansible_bootstrap_flags = --user="miner" --ask-pass --ask-become-pass

.PHONY: all
all: init install check

-include install-git-hooks
.PHONY: install-git-hooks

install-git-hooks: .git/hooks/pre-commit

.git/hooks/pre-commit:
	ln -sf ../../hooks/pre-commit $@

.PHONY: init
init:
	pip install -i $(PIP_INDEX_URL) -U setuptools pip pip-tools

.PHONY: install
install: requirements.txt
	pip-sync -i $(PIP_INDEX_URL)
	mkdir -p roles/galaxy
	ansible-galaxy install -r requirements.yml --roles-path roles/ --force

upgrade = $(or UPGRADE,0)
ifneq ($(upgrade),0)
pip_compile_flags += --upgrade --rebuild
endif
pip_compile = pip-compile -i $(PIP_INDEX_URL) $(pip_compile_flags)

requirements.txt: requirements.in
	$(pip_compile) $< -o requirements.txt

.PHONY: check
check:
	$(info checking playbook $(playbook_file) syntax against inventory $(inventory_file))
	$(ansible_playbook_cmd) --syntax-check

.PHONY: bootstrap
bootstrap:
	$(ansible_playbook_cmd) $(ansible_bootstrap_flags) --extra-vars=bootstrap=yes --tags=bootstrap --skip-tags=base,base-setup

.PHONY: connect
connect:
	$(ansible_playbook_cmd) --tags=connect --skip-tags=base

.PHONY: known-hosts
known-hosts: override playbook = include-setup
known-hosts: hosts = acc
known-hosts:
	$(ansible_playbook_cmd) --extra-vars=setup_hosts=all

.PHONY: run
run:
	$(ansible_playbook_cmd)

.PHONY: dry-run
dry-run:
	$(ansible_playbook_cmd) --check

.PHONY: facts
facts:
	$(ansible_setup) all

vault_marker := $$ANSIBLE_VAULT;
vault_files_encrypted = $(if $(vault_files),$(shell grep --files-with-matches '$(vault_marker)' $(vault_files)))
vault_files_decrypted = $(if $(vault_files),$(shell grep --files-without-match '$(vault_marker)' $(vault_files)))
vault_files_decrypted_staged = $(if $(vault_files),$(shell git grep --cached --files-without-match '$(vault_marker)' $(vault_files)))

.PHONY: vault-ls
vault-ls:
	@$(foreach file,$(vault_files),\
		echo '$(file)';)

.PHONY: vault-ls-encrypted
vault-ls-encrypted:
	@$(foreach file,$(vault_files_encrypted),\
		echo '$(file)';)

.PHONY: vault-ls-decrypted
vault-ls-decrypted:
	@$(foreach file,$(vault_files_decrypted),\
		echo '$(file)';)

.PHONY: vault-decrypt
vault-decrypt:
	$(if $(vault_files_encrypted),ansible-vault decrypt -v --vault-password-file=$(vault_password_file) $(vault_files_encrypted))

.PHONY: vault-encrypt
vault-encrypt:
	$(if $(vault_files_decrypted),ansible-vault encrypt -v --vault-password-file=$(vault_password_file) $(vault_files_decrypted))

.PHONY: vault-check
vault-check:
	$(if $(vault_files_decrypted_staged),\
			cat hooks/nope >&2; echo 'must encrypt vault files by running make vault-encrypt' >&2; exit 1;\
	)

