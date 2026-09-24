# SPDX-FileCopyrightText: 2026 Curtis Galloway
# SPDX-License-Identifier: Apache-2.0
# pylint: disable=invalid-name
"""`python -m fx_updater`: the entry point the installed systemd unit runs."""

import sys

from fx_updater.cli import main

sys.exit(main())
