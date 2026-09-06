"""Command-line entry point for the loopback-only local server."""

import uvicorn

from aero_bot.config import Settings


def main() -> None:
    """Start Aero Bot using validated local-only bind settings."""
    # Settings validation rejects non-loopback hosts before the server starts.
    settings = Settings()
    uvicorn.run(
        "aero_bot.app:create_app",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
    )


if __name__ == "__main__":
    main()
