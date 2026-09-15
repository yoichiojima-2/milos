.PHONY: console test

# Build the web console (Node 22 required); output lands in
# src/milos/console/static/, which is gitignored — the Docker build produces
# its own copy, so this is only needed for a local `milos serve api`.
console:
	cd console && npm install && npm run build

test:
	uv run pytest -q
