.PHONY: test test-cov lint format clean install

install:
	pip install -e ".[dev]"

test:
	pytest tests/ -v --tb=short

test-cov:
	pytest tests/ -v --tb=short --cov=harness --cov-report=term-missing

lint:
	ruff check harness/

format:
	ruff format harness/

clean:
	rm -rf sessions/
	rm -rf .pytest_cache/
	rm -rf __pycache__ harness/**/__pycache__ tests/__pycache__
	find . -name "*.pyc" -delete
	find . -name "__pycache__" -type d -delete
