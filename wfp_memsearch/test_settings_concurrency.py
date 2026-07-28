"""Dedicated settings for file-backed concurrency tests.

These tests must run with:
    python manage.py test roster.tests.concurrency \
        --settings=wfp_memsearch.test_settings_concurrency

The concurrency tests are excluded from default discovery.
"""
from .settings import *  # noqa: F401, F403
import tempfile
import os

CONCURRENCY_TEST_DIR = tempfile.mkdtemp(prefix='wfp_concurrency_')
CONCURRENCY_TEST_DB_PATH = os.path.join(CONCURRENCY_TEST_DIR, 'test_concurrency.sqlite3')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': CONCURRENCY_TEST_DB_PATH,
        'TEST': {
            'NAME': CONCURRENCY_TEST_DB_PATH,
        },
    }
}

CONCURRENCY_TESTS_ENABLED = True
