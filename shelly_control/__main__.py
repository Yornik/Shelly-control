"""Entry point: run the controller, and serve the page, the API and /metrics."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from .config import Config
from .controller import Controller
from .logs import configure, event
from .metrics import make_registry
from .prices import PriceClient
from .shelly import ShellyClient
from .store import Store
from .web import create_app

LOG = logging.getLogger("shelly-control")


def build(config: Config) -> tuple[Controller, object]:
    """The controller and the WSGI app, wired from a validated config."""
    controller = Controller(
        shelly=ShellyClient(
            config.shelly_url, config.shelly_password, timeout=config.shelly_timeout
        ),
        prices=PriceClient(config.exporter_url, config.home_id, timeout=config.exporter_timeout),
        store=Store(config.state_dir),
        tz=config.tz,
        defaults=config.defaults,
        switch_id=config.switch_id,
        allow_switching=config.allow_switching,
        evaluation_delay=config.evaluation_delay,
        price_refresh=config.price_refresh,
        min_switch_interval=config.min_switch_interval,
        log_size=config.decision_log_size,
    )
    app = create_app(
        controller,
        tz=config.tz,
        allowed_hosts=config.allowed_hosts,
        display_name=config.display_name,
        registry=make_registry(controller, config.tz),
    )
    return controller, app


def serve(config: Config) -> None:  # pragma: no cover - process wiring
    import waitress

    controller, app = build(config)
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
        controller.stop()

    # As PID 1 in a container, Python ignores SIGTERM unless it has a handler,
    # and the pod would wait out its whole grace period.
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    server = waitress.create_server(
        app,
        listen=f"0.0.0.0:{config.listen_port}",  # noqa: S104 - reached through a Service
        threads=4,
        ident=None,
        # Waitress spools a larger body to a temporary file, which a read-only
        # root does not have: the request would break instead of getting a 413.
        max_request_body_size=65536,
        clear_untrusted_proxy_headers=True,
    )
    threading.Thread(target=server.run, name="http", daemon=True).start()
    snap = controller.snapshot()
    event(
        LOG,
        "start",
        f"serving the page, the API and /metrics on :{config.listen_port}",
        mode=snap.settings.mode,
        switching_allowed=config.allow_switching,
        shelly=config.shelly_url,
        password=bool(config.shelly_password),
        exporter=config.exporter_url,
        timezone=config.timezone,
        state_dir=config.state_dir,
        allowed_hosts=config.allowed_hosts,
    )
    try:
        controller.run(stop)
    finally:
        event(LOG, "stop", "stopping")
        server.close()


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process wiring
    parser = argparse.ArgumentParser(
        prog="shelly-control",
        description="Switch a Shelly Gen2 relay by the electricity price. Configured with "
        "environment variables; see the README.",
    )
    parser.parse_args(argv)
    configure()
    config = Config()
    config.validate()
    serve(config)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
