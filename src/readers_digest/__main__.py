"""`python -m readers_digest`: the command line, as the window runs it.

Also the root __main__.py of readers-digest.pyz (release.yml), so it must
not import from its own package relatively.
"""

import sys

from readers_digest.cli import main

sys.exit(main())
