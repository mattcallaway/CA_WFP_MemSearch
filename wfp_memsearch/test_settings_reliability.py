"""Dedicated settings for reliability benchmarks and evidence commands.

This module configures an isolated file-backed SQLite database for
benchmarks, provenance traces, repair rehearsals, and destructive validation.

The database path is read from the WFP_RELIABILITY_DB_PATH environment variable.
The reliability runner sets this before launching subprocesses.
"""
from .settings import *  # noqa: F401, F403
import os

WFP_RELIABILITY_DB_PATH = os.environ.get('WFP_RELIABILITY_DB_PATH', '')

if not WFP_RELIABILITY_DB_PATH:
    raise RuntimeError(
        'WFP_RELIABILITY_DB_PATH environment variable must be set. '
        'Use the reliability runner to launch commands with isolated databases.'
    )

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': WFP_RELIABILITY_DB_PATH,
    }
}

# Disable caching during benchmarks
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.dummy.DummyCache',
    }
}
