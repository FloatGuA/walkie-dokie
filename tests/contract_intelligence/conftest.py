"""Initialize Django only when contract-intelligence tests are collected."""

import os

os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE",
    "walkie_dokie.contract_admin.settings",
)

import django

django.setup()
