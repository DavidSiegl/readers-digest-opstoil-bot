"""The desktop window (window), its tray icon (tray), and what they show (model).

Needs PyGObject with GTK 4 and libadwaita, which distributions ship for
their own Python: `readers-digest gui` runs the window in its own process
when it can import them, and otherwise on /usr/bin/python3 (cli.gui).
The window imports nothing outside the standard library, PyGObject and
this package, and runs sync and review as readers-digest subprocesses.
"""
