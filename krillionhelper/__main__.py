"""Entry point: python -m krillionhelper [--recalibrate] [--port N] [--config PATH]"""

import argparse
import os
import sys
import webbrowser
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, PROJECT_DIR, loadConfig, saveConfig


def loadDotEnv():
    """Fill ANTHROPIC_API_KEY / TYPESAFE_API_KEY from ./.env when they aren't set in the shell."""
    path = PROJECT_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        name, sep, value = line.strip().removeprefix("export ").partition("=")
        name = name.strip()
        if sep and name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY") and not os.environ.get(name):
            os.environ[name] = value.strip().strip("'\"")


def main():
    parser = argparse.ArgumentParser(prog="krillionhelper", description=__doc__)
    parser.add_argument("--recalibrate", action="store_true", help="open the page in region-selection mode")
    parser.add_argument("--overlay-calibration", action="store_true", help="pick the region with a full-screen overlay instead of the page")
    parser.add_argument("--port", type=int, help="web page port (default 8765)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.json")
    parser.add_argument("--no-watch", action="store_true", help="don't watch the screen; manual prompts only")
    parser.add_argument("--no-open", action="store_true", help="don't open the browser")
    args = parser.parse_args()

    configPath = Path(args.config)
    cfg = loadConfig(configPath)
    if args.port:
        cfg["port"] = args.port

    if args.overlay_calibration:
        from .calibrate import selectRegion

        region = selectRegion()
        if region:
            cfg["region"] = region
            saveConfig(cfg, configPath)
            print(f"Saved region {region} to {configPath}", flush=True)

    loadDotEnv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Export it, or put ANTHROPIC_API_KEY=... in .env next to config.json.")

    from .app import TriviaApp
    from .server import Broadcaster, createServer

    broadcaster = Broadcaster()
    app = TriviaApp(cfg, broadcaster.publish, configPath)
    server = createServer(app, broadcaster, cfg["port"])
    url = f"http://localhost:{cfg['port']}"
    # No region yet, or asked to redo it: open the page straight into region selection.
    openUrl = url + "/#select" if (args.recalibrate or not cfg.get("region")) and not args.no_watch else url
    app.start(watch=not args.no_watch)
    print(f"Answers page: {url}  (Ctrl+C to quit)", flush=True)
    if not cfg.get("region") and not args.no_watch:
        print("No screen region yet: click 'Select region' on the page.", flush=True)
    if not args.no_open:
        webbrowser.open(openUrl)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()


if __name__ == "__main__":
    main()
