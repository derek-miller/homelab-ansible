PIP_INDEX_URL = https://pypi.org/simple

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

vault_files ?= $(shell find playbooks kubernetes -type f -path '*vault*' -not -path 'playbooks/collections/*' -not -path 'playbooks/roles/galaxy/*')
vault_tiers := infra app
vault_flag = $(strip $(foreach t,$(vault_tiers),$(if $(wildcard .vault_pass_$(t)),--vault-id=$(t)@.vault_pass_$(t))) $(if $(wildcard .vault_pass),--vault-id=.vault_pass))
# Encrypting each file to its own tier, and checking labels, waits until every vault file has been re-keyed to one.
vault_tiered ?= no
base ?= HEAD

ansible_default_flags = --inventory=$(inventory_file) $(vault_flag)

ansible_flags ?= $(or $(ANSIBLE_FLAGS),$(ANSIBLE_OPTS))
override ansible_flags += $(if $(hosts),--limit='$(hosts)')
comma:= ,
override ansible_flags += $(if $(tags),--skip-tags=base$(comma)common --tags='$(tags)')

ansible_playbook_cmd_fn = ansible-playbook $(1) $(ansible_default_flags) $(ansible_flags)
ansible_playbook_cmd = $(call ansible_playbook_cmd_fn,$(playbook_file))
ansible_setup = ansible -m setup $(ansible_default_flags) $(ansible_flags)

ansible_bootstrap_flags = $(if $(user),--user=$(user)) --ask-pass $(if $(become_pass),--ask-become-pass)

.PHONY: all
all: after-git-pull check

.PHONY: after-git-pull
after-git-pull: init install galaxy-install install-git-hooks

git_hook := $(shell git rev-parse --git-common-dir)/hooks/pre-commit

.PHONY: install-git-hooks
install-git-hooks: $(git_hook)

$(git_hook):
	ln -sf ../../hooks/pre-commit $@

.PHONY: init
init:
	pip install -i $(PIP_INDEX_URL) -U setuptools pip wheel pip-tools

.PHONY: install
install: requirements.txt playbooks/roles/ansible/remote/files/requirements.txt playbooks/roles/generic/tplink-plug-stats/files/requirements.txt
	pip-sync -i $(PIP_INDEX_URL)

.PHONY: galaxy-install
galaxy-install: playbooks/roles/requirements.yml playbooks/collections/requirements.yml
	rm -rf playbooks/roles/galaxy playbooks/collections/ansible_collections
	mkdir -p playbooks/roles/galaxy
	ansible-galaxy role install --force -r playbooks/roles/requirements.yml --roles-path playbooks/roles/
	ansible-galaxy collection install --force -r playbooks/collections/requirements.yml --collections-path playbooks/collections/

upgrade = $(or $(UPGRADE),0)
ifneq ($(upgrade),0)
pip_compile_flags += --upgrade --rebuild
endif
pip_compile = pip-compile -i $(PIP_INDEX_URL) --no-strip-extras $(pip_compile_flags)

requirements.txt: requirements.in
	$(pip_compile) $< -o $@

playbooks/roles/ansible/remote/files/requirements.txt: playbooks/roles/ansible/remote/files/requirements.in
	$(pip_compile) $< -o $@

playbooks/roles/generic/tplink-plug-stats/files/requirements.txt: playbooks/roles/generic/tplink-plug-stats/files/requirements.in
	$(pip_compile) $< -o $@

.PHONY: lint
lint:
	yamllint --strict .

# Role test suites are plain-stdlib scripts, so they run in the same minimal
# venv CI's lint job builds. Three review rounds of watchdog findings were all
# expressible in this suite; it is a gate, not a courtesy.
.PHONY: test
test:
	@set -e; found=0; for t in $$(find playbooks/roles -path '*/tests/test_*.py' -not -path 'playbooks/roles/galaxy/*' | sort); do \
		echo "== $$t"; python3 "$$t"; found=1; \
	done; [ "$$found" = 1 ] || { echo "no test suites found"; exit 1; }

.PHONY: check
check: lint test
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

# <name> selects playbooks/<name>.yml, paired with playbooks/hosts-<name>
# when that inventory exists.
bootstrap-% run-% dry-run-% check-%: playbook = $*
bootstrap-% run-% dry-run-% check-%: inventory = $(if $(wildcard playbooks/hosts-$*),hosts-$*,hosts)

bootstrap-%:
	$(ansible_playbook_cmd) $(ansible_bootstrap_flags) --extra-vars=bootstrap=yes --tags=bootstrap --skip-tags=base,common

run-%:
	$(ansible_playbook_cmd)

dry-run-%:
	$(ansible_playbook_cmd) --check

check-%:
	$(ansible_playbook_cmd) --syntax-check

.PHONY: facts
facts:
	$(ansible_setup) all

vault_marker := $$ANSIBLE_VAULT;
vault_files_encrypted = $(if $(vault_files),$(shell grep --files-with-matches '$(vault_marker)' $(vault_files)))
vault_files_decrypted = $(if $(vault_files),$(shell grep --files-without-match '$(vault_marker)' $(vault_files)))
vault_files_decrypted_staged = $(if $(vault_files),$(shell git grep --cached --files-without-match '$(vault_marker)' $(vault_files)))
# The labels $(vault_flag) loads; an unlabelled --vault-id is Ansible's "default".
vault_ids = $(foreach f,$(filter --vault-id=%,$(vault_flag)),$(if $(findstring @,$(f)),$(firstword $(subst @, ,$(patsubst --vault-id=%,%,$(f)))),default))
vault_id_loaded = printf ' %s ' '$(vault_ids)' | grep -q " $(1) "
# In a shell loop over $$file: skip a file whose header names a password that isn't loaded, so one file can't stop the rest.
vault_skip_unloaded = label=$$(head -n 1 "$$file" | cut -d ';' -f 4); \
	if [ -n "$$label" ] && ! $(call vault_id_loaded,$$label); then \
		echo "Skipping $$file: $$label tier, its password isn't loaded" >&2; continue; \
	fi;
vault_encrypt_tiered = for file in $(vault_files_decrypted); do \
		case "$$file" in *infra*) tier=infra ;; *) tier=app ;; esac; \
		if $(call vault_id_loaded,$$tier); then \
			ansible-vault encrypt -v $(vault_flag) --encrypt-vault-id=$$tier "$$file" || exit 1; \
		else \
			echo "Skipping $$file: $$tier tier, its password isn't loaded" >&2; \
		fi; \
	done
# Labels are plaintext, so these need no password: whole-file vaults and !vault values in infra.yml carry infra,
# and everything else carries app.
vault_label_check = bad=; \
	for file in $$(git grep --cached -l '^\$$ANSIBLE_VAULT;' -- $(vault_files)); do \
		case "$$file" in *infra*) want=infra ;; *) want=app ;; esac; \
		[ "$$(git show :"$$file" | head -n 1)" = '$$ANSIBLE_VAULT;1.2;AES256;'"$$want" ] \
			|| { echo "$$file: must be encrypted with the $$want vault ID" >&2; bad=1; }; \
	done; \
	for file in $$(git ls-files -- 'playbooks/group_vars/*' 'playbooks/host_vars/*'); do \
		case "$$file" in */infra.yml) want=infra ;; *) want=app ;; esac; \
		git show :"$$file" | head -n 1 | grep -q '^\$$ANSIBLE_VAULT;' && continue; \
		if git show :"$$file" | grep -o '\$$ANSIBLE_VAULT;[^ ]*' | grep -v -x '\$$ANSIBLE_VAULT;1\.2;AES256;'"$$want" | grep -q .; then \
			echo "$$file: every !vault value must be encrypted with the $$want vault ID" >&2; bad=1; \
		fi; \
	done; \
	[ -z "$$bad" ]

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
	@files=; for file in $(vault_files_encrypted); do \
		$(vault_skip_unloaded) \
		files="$$files $$file"; \
	done; \
	if [ -n "$$files" ]; then ansible-vault decrypt -v $(vault_flag) $$files; fi

.PHONY: vault-encrypt
vault-encrypt: vault-revert-unchanged
	$(if $(vault_files_decrypted),$(if $(filter yes,$(vault_tiered)),$(vault_encrypt_tiered),ansible-vault encrypt -v $(vault_flag) $(if $(word 2,$(vault_ids)),--encrypt-vault-id=default) $(vault_files_decrypted)))

.PHONY: vault-revert-unchanged
vault-revert-unchanged:
	@for file in $(vault_files_decrypted); do \
		head_tmp=$$(mktemp); \
		git show HEAD:"$$file" > "$$head_tmp" 2>/dev/null \
			&& ansible-vault decrypt $(vault_flag) "$$head_tmp" 2>/dev/null \
			|| : > "$$head_tmp"; \
		if diff -qw "$$head_tmp" "$$file" > /dev/null 2>&1 && [ -s "$$head_tmp" ]; then \
			echo "Reverting unchanged vault file: $$file"; \
			git checkout HEAD -- "$$file"; \
		else \
			echo "Leaving changed vault file decrypted: $$file"; \
		fi; \
		rm -f "$$head_tmp"; \
	done

.PHONY: vault-check
vault-check:
	$(if $(vault_files_decrypted_staged),\
		cat hooks/nope >&2; echo 'must encrypt vault files by running make vault-encrypt' >&2; exit 1;\
	)
	$(if $(filter yes,$(vault_tiered)),@$(vault_label_check))

.PHONY: vault-diff
vault-diff:
	@git rev-parse --verify --quiet '$(base)^{commit}' > /dev/null \
		|| { echo "vault-diff: base '$(base)' is not a commit" >&2; exit 1; }
	@for file in $(vault_files); do \
		head_tmp=$$(mktemp); \
		curr_tmp=$$(mktemp); \
		unreadable=; \
		if ! git show $(base):"$$file" > "$$head_tmp" 2>/dev/null; then \
			: > "$$head_tmp"; \
		elif grep -q '$(vault_marker)' "$$head_tmp" && ! ansible-vault decrypt $(vault_flag) "$$head_tmp" 2>/dev/null; then \
			unreadable=$$(head -n 1 "$$head_tmp" | cut -d ';' -f 4); unreadable=$${unreadable:-unlabelled}; \
		fi; \
		if [ ! -f "$$file" ]; then \
			: > "$$curr_tmp"; \
		elif grep -q '$(vault_marker)' "$$file" 2>/dev/null; then \
			ansible-vault view $(vault_flag) "$$file" > "$$curr_tmp" 2>/dev/null \
				|| { unreadable=$$(head -n 1 "$$file" | cut -d ';' -f 4); unreadable=$${unreadable:-unlabelled}; }; \
		else \
			cat "$$file" > "$$curr_tmp" 2>/dev/null; \
		fi; \
		if [ -n "$$unreadable" ]; then \
			git show $(base):"$$file" 2>/dev/null | cmp -s - "$$file" \
				|| echo "$$file: changed, $$unreadable tier, not readable with these passwords"; \
		elif ! diff -qw "$$head_tmp" "$$curr_tmp" > /dev/null 2>&1; then \
			diff -uw --label "a/$$file" --label "b/$$file" "$$head_tmp" "$$curr_tmp" || true; \
		fi; \
		rm -f "$$head_tmp" "$$curr_tmp"; \
	done
