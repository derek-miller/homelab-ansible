PIP_INDEX_URL = https://pypi.python.org/simple

ifeq ($(VIRTUAL_ENV),)
$(error must run in a virtualenv)
else
$(info running in virtualenv $(VIRTUAL_ENV))
endif

playbook ?= $(or $(ANSIBLE_PLAYBOOK),$(PLAYBOOK),default)
inventory ?= $(or $(ANSIBLE_INVENTORY),$(INVENTORY),hosts)

inventory_file_maybe = playbooks/$(inventory)
override inventory_file = $(or $(wildcard $(inventory_file_maybe)),$(error could not find inventory file $(inventory_file_maybe)))

playbook_file_maybe = playbooks/$(playbook).yml
override playbook_file = $(or $(wildcard $(playbook_file_maybe)),$(error could not find playbook file $(playbook_file_maybe)))

vault_files ?= $(shell find playbooks -type f -path '*vault*' -not -path '*vault-plaintext*' -not -path 'playbooks/collections/*' -not -path 'playbooks/roles/galaxy/*')
vault_flag = --vault-id=.vault_pass

ansible_default_flags = --inventory=$(inventory_file) $(vault_flag)

ansible_flags ?= $(or $(ANSIBLE_FLAGS),$(ANSIBLE_OPTS))
override ansible_flags += $(if $(hosts),--limit='$(hosts)')
comma:= ,
override ansible_flags += $(if $(tags),--skip-tags=base$(comma)common --tags='$(tags)')

ansible_playbook_cmd_fn = ansible-playbook $(1) $(ansible_default_flags) $(ansible_flags)
ansible_playbook_cmd = $(call ansible_playbook_cmd_fn,$(playbook_file))
ansible_setup = ansible -m setup $(ansible_default_flags) $(ansible_flags)

ansible_bootstrap_flags = --user=$(user) --ask-become-pass --ask-pass

python_version_full := $(wordlist 2,4,$(subst ., ,$(shell python --version 2>&1)))
python_version_major := $(word 1,${python_version_full})
python_version_minor := $(word 2,${python_version_full})

.PHONY: all
all: after-git-pull check

.PHONY: after-git-pull
after-git-pull: init install galaxy-install

-include install-git-hooks
.PHONY: install-git-hooks

install-git-hooks: .git/hooks/pre-commit

.git/hooks/pre-commit:
	ln -sf ../../hooks/pre-commit $@

.PHONY: init
init:
	pip install -i $(PIP_INDEX_URL) -U setuptools pip wheel pip-tools

.PHONY: install
install: requirements.txt playbooks/roles/ansible/remote/files/requirements.txt
	pip-sync -i $(PIP_INDEX_URL)

.PHONY: galaxy-install
galaxy-install: playbooks/roles/requirements.yml playbooks/collections/requirements.yml
	mkdir -p playbooks/roles/galaxy
	ansible-galaxy role install --force -r playbooks/roles/requirements.yml --roles-path playbooks/roles/
	ansible-galaxy collection install --force -r playbooks/collections/requirements.yml --collections-path playbooks/collections/

upgrade = $(or UPGRADE,0)
ifneq ($(upgrade),0)
pip_compile_flags += --upgrade --rebuild
endif
pip_compile = pip-compile -i $(PIP_INDEX_URL) --no-strip-extras $(pip_compile_flags)

requirements.txt: requirements.in
	$(pip_compile) $< -o $@

playbooks/roles/ansible/remote/files/requirements.txt: playbooks/roles/ansible/remote/files/requirements.in
	$(pip_compile) --pip-args='--no-build-isolation' $< -o $@

.PHONY: fmt
fmt:
	black . --line-length 100 --target-version py$(python_version_major)$(python_version_minor)

.PHONY: check
check:
	$(ansible_playbook_cmd) --syntax-check

.PHONY: bootstrap
bootstrap:
	$(ansible_playbook_cmd) $(ansible_bootstrap_flags) --extra-vars=bootstrap=yes --tags=bootstrap --skip-tags=base,common

.PHONY: connect
connect:
	$(ansible_playbook_cmd) --tags=connect --skip-tags=base,common

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
	$(if $(vault_files_encrypted),ansible-vault decrypt -v $(vault_flag) $(vault_files_encrypted))


.PHONY: vault-encrypt
vault-encrypt:
	$(if $(vault_files_decrypted),ansible-vault encrypt -v $(vault_flag) $(vault_files_decrypted))

.PHONY: vault-check
vault-check:
	$(if $(vault_files_decrypted_staged),\
		cat hooks/nope >&2; echo 'must encrypt vault files by running make vault-encrypt-all' >&2; exit 1;\
	)

.PHONY: docker-prune
docker-prune:
	ansible docker $(ansible_default_flags) $(ansible_flags) -m shell -a "docker system prune -f"
