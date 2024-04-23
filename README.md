# Basic Usage

```python3
>>> import rrtask
```

Refer the convetion's [documentation on notion](https://www.notion.so/dolead/New-Naming-Convention-2023-b656d1a7179c42de97332051d9abb3fa) for detail about the behavior.

# Install

via `pip`:

```shell
pip install rrtask --extra-index-url https://pypi.dolead.sh/simple
```

via `pipenv`:

```
pipenv install rrtask --index dolead
```

# Dev

Setup poetry repo:

```shell
poetry config repositories.dolead  http://apypi01.prod.dld/
```

Bump version in pyproject.toml and push with `make deploy`
