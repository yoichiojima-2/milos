.PHONY: test deploy

IMAGE  ?= asia-northeast1-docker.pkg.dev/$(PROJECT)/milos/runner:latest
REGION ?= asia-northeast1
JOB    ?= milos-runner

test:
	uv run pytest tests/ -q
	uv run ruff check . && uv run ruff format --check .

# Build and push the runner image, then re-pin the Cloud Run job to the new
# digest — pushing :latest alone is not enough, Cloud Run resolves the tag to
# a digest at deploy time, not per execution.
deploy:
	docker build --platform linux/amd64 -t $(IMAGE) .
	docker push $(IMAGE)
	gcloud run jobs update $(JOB) --image $(IMAGE) --region $(REGION)
