#!/usr/bin/env python3
"""Django management entry point for the local contract-intelligence admin."""

import os
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
os.environ.setdefault(
    "DJANGO_SETTINGS_MODULE", "walkie_dokie.contract_admin.settings"
)

from django.core.management import execute_from_command_line


if __name__ == "__main__":
    execute_from_command_line(sys.argv)
