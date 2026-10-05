PYTHON := python

test: ## Run the test suite
	@$(PYTHON) -m pytest -q tests

lint: ## Lint and format
	@$(PYTHON) -m ruff check --fix
	@$(PYTHON) -m ruff format .

build: ## Build the sdist and wheel
	@$(PYTHON) -m build
