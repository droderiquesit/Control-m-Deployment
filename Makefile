# Control-M platform toolchain helpers (Bicep + Python ctmdeploy + Packer).
#   make whatif ENV=nonprod
#   make deploy ENV=prod [PHASE=compute]
#   make image VERSION=1.0.0
ENV    ?= nonprod
PHASE  ?= all
VERSION ?=
PY     ?= python3
BICEP  ?= bicep
CTM     = $(PY) -m ctmdeploy

.PHONY: help install image gallery whatif deploy secrets failover render fmt lint test clean

help:
	@echo "Targets: install image gallery whatif deploy secrets failover fmt lint test clean"
	@echo "Usage:   make whatif ENV=<nonprod|prod>"
	@echo "         make deploy ENV=<nonprod|prod> [PHASE=all|rg|foundation|secrets|compute|dr|replication]"
	@echo "         make image VERSION=x.y.z [ENV=prod] [IMAGE=base|agent MEDIA=dir]  (packer -> Compute Gallery)"
	@echo "         make failover ENV=prod                (DR runbook: docs/DR-RUNBOOK.md)"

install:
	$(PY) -m pip install -e "deploy/[dev]"

# Golden images: packer build -> Compute Gallery version (immutable; bump,
# never rebuild). IMAGE=base (default, ctm-rhel9 OS-only) or IMAGE=agent
# (ctm-agent with the Control-M/Agent baked in; needs MEDIA=<dir>).
IMAGE ?= base
MEDIA ?=
image:
	@test -n "$(VERSION)" || { echo "usage: make image VERSION=x.y.z [ENV=prod] [IMAGE=base|agent MEDIA=dir]"; exit 2; }
	$(CTM) image build --env $(ENV) --version $(VERSION) --image $(IMAGE) $(if $(MEDIA),--media-path $(MEDIA),)

# One-time (idempotent): ctm-images-rg + ctmgallery + the ctm-rhel9 and
# ctm-agent definitions.
gallery:
	$(CTM) image gallery --env $(ENV)

whatif:
	$(CTM) whatif --env $(ENV)

deploy:
	$(CTM) deploy --env $(ENV) --phase $(PHASE)

secrets:
	$(CTM) secrets --env $(ENV)

failover:
	$(CTM) failover --env $(ENV)

render:
	$(CTM) render --env $(ENV) --role em --allow-placeholders

fmt:
	@find bicep -name '*.bicep' -print -exec $(BICEP) format {} \;

lint:
	@rc=0; for f in $$(find bicep -name '*.bicep' | sort); do \
	  echo "== bicep lint $$f"; $(BICEP) lint "$$f" || rc=1; \
	done; \
	$(PY) -m ruff check deploy || rc=1; \
	exit $$rc

test:
	$(PY) -m pytest deploy/tests -q

clean:
	find . -type d \( -name "__pycache__" -o -name ".pytest_cache" -o -name "*.egg-info" -o -name ".ruff_cache" -o -name "packer_cache" \) -prune -exec rm -rf {} +
	find bicep -name "*.json" -delete
