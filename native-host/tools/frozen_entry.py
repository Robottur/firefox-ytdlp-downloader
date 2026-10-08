"""PyInstaller entry point; keep package-relative imports intact."""
from ytdlp_host.bootstrap import main

if __name__ == '__main__':
    raise SystemExit(main())
