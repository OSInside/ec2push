clean:
	rm -rf dist

setup:
	poetry install --all-extras

pypi: clean setup
	poetry build --format=sdist
