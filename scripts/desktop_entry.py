"""Thin PyInstaller adapter for the canonical application entrypoint."""
from app.entrypoints.server import desktop_main


if __name__ == "__main__":
    desktop_main()
