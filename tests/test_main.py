"""Behavior tests for the local command-line server boundary."""

from unittest.mock import patch

from aero_bot.__main__ import main


def test_main_starts_factory_on_loopback() -> None:
    """The console command starts the application factory on a loopback address."""
    # The server runner is replaced so the test verifies startup without blocking.
    with patch("aero_bot.__main__.uvicorn.run") as run_server:
        main()

    run_server.assert_called_once_with(
        "aero_bot.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8765,
    )
