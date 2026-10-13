# Control-M platform IaC helper targets.
#   make init ENV=dev
#   make plan ENV=prod
#   make apply ENV=uat
ENV ?= dev
TF  ?= terraform
DIR  = environments/$(ENV)
ENVS = dev qa uat prod

.PHONY: help bootstrap init plan apply destroy fmt validate lint output clean

help:
	@echo "Targets: bootstrap init plan apply destroy fmt validate lint output clean"
	@echo "Usage:   make <target> ENV=<dev|qa|uat|prod>"

bootstrap:
	$(TF) -chdir=bootstrap init && $(TF) -chdir=bootstrap apply

init:
	$(TF) -chdir=$(DIR) init -upgrade -reconfigure -backend-config=backend.hcl

plan: init
	$(TF) -chdir=$(DIR) plan -out=$(ENV).tfplan

apply:
	$(TF) -chdir=$(DIR) apply $(ENV).tfplan

destroy:
	$(TF) -chdir=$(DIR) destroy

fmt:
	$(TF) fmt -recursive

validate:
	@for e in $(ENVS); do \
	  echo "== validate $$e"; \
	  $(TF) -chdir=environments/$$e init -backend=false -input=false >/dev/null && \
	  $(TF) -chdir=environments/$$e validate || exit 1; \
	done

lint:
	tflint --recursive --config "$(CURDIR)/.tflint.hcl"

output:
	$(TF) -chdir=$(DIR) output

clean:
	find . -name "*.tfplan" -delete
	find . -type d -name ".terraform" -prune -exec rm -rf {} +
