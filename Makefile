.PHONY: test lint typecheck fmt validate-agents tf-fmt tf-validate image

ENV    ?= dev
REGION ?= asia-northeast1
TAG    ?= $(shell git rev-parse --short HEAD)

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

typecheck:
	uv run mypy

fmt:
	uv run ruff check --fix . && uv run ruff format .

validate-agents:
	uv run milos agents validate agents/*.yaml deployments/*/*.yaml

tf-fmt:
	terraform fmt -recursive -check infra

tf-validate:
	terraform -chdir=infra/envs/$(ENV) init -backend=false -input=false >/dev/null
	terraform -chdir=infra/envs/$(ENV) validate

# Build in Cloud Build and push to the runtime project's registry, then pass
# the tag to Terraform as var.image. REPO comes from `terraform output image_repository`.
image:
	gcloud builds submit --tag $(REPO)/milos:$(TAG) .
