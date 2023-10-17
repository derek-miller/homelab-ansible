UPGRADE ?= 0
ifneq ($(UPGRADE),0)
pip_compile_flags += --upgrade --rebuild
endif
pip_compile = pip-compile $(pip_compile_flags) --pip-args='--no-build-isolation'

requirements.txt: requirements.in
	$(pip_compile) requirements.in
