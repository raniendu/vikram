"""Entry point for the app's shared frozen Python runtime."""

import json
import sys
from pathlib import Path


def main() -> int:
    if sys.argv[1:2] == ["--check-sandbox"]:
        from vikram.sandbox import check_sandbox

        return check_sandbox()
    if sys.argv[1:2] == ["--check-browser"]:
        from vikram.browser import check_browser

        return check_browser()
    if sys.argv[1:] == ["--run-schedules"]:
        # The background agent: run due scheduled tasks, then exit.
        from vikram.schedules import main as schedules_main

        return schedules_main()
    if sys.argv[1:2] == ["--cli"]:
        from vikram.cli import main as cli_main

        return cli_main(sys.argv[2:])
    if sys.argv[1:2] == ["--install-bundle"]:
        from vikram.installation import InstallError, Layout, install_bundle

        if len(sys.argv) != 3:
            sys.stderr.write("Expected an application bundle to install.\n")
            return 1
        try:
            layout = Layout(Path.home())
            version = install_bundle(Path(sys.argv[2]), layout)
            print(
                json.dumps(
                    {"version": version, "app": str(layout.app), "cli": str(layout.cli)}
                )
            )
            return 0
        except (InstallError, OSError) as exc:
            sys.stderr.write(f"Vikram was not installed: {exc}\n")
            return 1
        except KeyboardInterrupt:
            return 130
    from vikram.desktop.__main__ import main as desktop_main

    desktop_main()
    return 0
