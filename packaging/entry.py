"""PyInstaller entry point.

Kept separate from ``desktop_client/main.py`` so the frozen build imports the
package normally instead of executing a module file as ``__main__``.
"""

from desktop_client.main import main

if __name__ == "__main__":
    main()
