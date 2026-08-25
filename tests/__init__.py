"""Test package.

This file is not decorative. Without it, ``tests.conftest`` is not importable
under a bare ``pytest`` invocation -- only under ``python -m pytest``, which
happens to put the working directory on ``sys.path``. Local runs passed and CI
failed on exactly that difference.
"""
