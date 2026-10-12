#!/usr/bin/env python3
"""Start the local web UI: python3 cos_ui.py [--port N] [--no-browser] [--allow-host NAME]."""
from cosmanagement.ui.server import main

if __name__ == '__main__':
    main()
