"""Start im Entwicklungsmodus bzw. Einstiegspunkt für PyInstaller."""
import multiprocessing

from jarvis.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
