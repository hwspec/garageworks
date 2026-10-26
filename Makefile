TARGETS=chiselsim
.PHONY: $(TARGETS) clean

VENV := .venv
PYTHON := $(VENV)/bin/python3

export PATH := $(abspath $(VENV)/bin):$(PATH)

all: $(TARGETS)

# Chisel test
chiselsim:
	@sbt "testOnly *"

#
# for setup
#
.PHONY: venv install install-bridge clean

venv:
	@python3 -m venv $(VENV)
	@$(PYTHON) -m pip install --upgrade pip

install: venv
	@$(PYTHON) -m pip install -r requirements.txt

install-bridge:
	@cd python && $(MAKE) install

setup: install install-bridge

clean:
	@rm -rf generated/*
	@sbt clean

distclean: clean
	@rm -rf $(VENV)
