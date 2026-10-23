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
	@echo "         make image VERSION=x.y.z [ENV=prod]   (packer -> Compute Gallery)"
	@echo "         make failover ENV=prod                (DR runbook: docs/DR-RUNBOOK.md)"

install:
	$(PY) -m pip install -e "deploy/[dev]"

# Golden image: packer build -> Compute Gallery version (immutable; bump, never rebuild).
image:
	@test -n "$(VERSION)" || { echo "usage: make image VERSION=x.y.z [ENV=prod]"; exit 2; }
	$(CTM) image build --env $(ENV) --version $(VERSION)

# One-time (idempotent): ctm-images-rg + ctmgallery + ctm-rhel9 definition.
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
