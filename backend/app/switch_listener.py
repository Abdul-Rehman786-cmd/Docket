"""Run with: python -m app.switch_listener [--check | --status]."""

import argparse
import asyncio
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.switch_listener import ListenerStopped, SwitchListener


async def execute(args) -> None:
    listener = SwitchListener(get_settings())
    if args.status or args.check:
        async with listener.http_client() as client:
            status = await listener.delivery_status(client)
        if args.check:
            covered = await listener.check_connection()
            print(f"API authentication OK. Switch connection OK. Configured room memberships verified: {covered}.")
        else:
            print(status.model_dump_json(indent=2))
    else:
        await listener.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward Switch message events to FastAPI.")
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--check", action="store_true", help="Check API authentication and Switch room membership; do not claim rooms or forward messages.")
    options.add_argument("--status", action="store_true", help="Show the latest sender name and message from the running API.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    configure_logging(get_settings())
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        print("Listener stopped.")
    except ListenerStopped as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except Exception:
        # Exceptions from HTTP/WebSocket libraries can contain credentials or message text.
        print("Listener could not continue. Check network access, configuration, and the API process.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
