RUN=poetry run
PROJECT_NAME=rrtask

test:
	$(RUN) pytest tests.py

clean:
	-rm -rf build dist .coverage test_coverage .mypy_cache .pytest_cache

lint:
	$(RUN) mypy $(PROJECT_NAME)/
	$(RUN) pycodestyle $(PROJECT_NAME)/
	$(RUN) pylint $(PROJECT_NAME)/ -d I0011,R0901,R0902,R0801,C0111,C0103,C0411,C0415,R0903,R0913,R0914,R0915,R1710,W0613,W0703

version:
	@echo -n 'VERSION = "' > $(PROJECT_NAME)/version.py
	poetry version -s | tr -d '\n' >> $(PROJECT_NAME)/version.py
	@echo '"' >> $(PROJECT_NAME)/version.py

build: clean version
	poetry check
	poetry build

deploy: test lint build
	-git tag --delete $(shell poetry version -s) 2> /dev/null
	git tag $(shell poetry version -s)
	poetry publish
	git push
	git push --tags
