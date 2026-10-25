"""Switch a Shelly Gen2 relay by the quarter-hour electricity price.

Reads prices from a tibber-exporter in the same cluster, decides with the
rules the owner sets in a small web interface, and switches the relay with
Switch.Set only. Starts in shadow mode, which decides and logs but never
switches.

Unofficial: not made, endorsed or supported by Shelly Group SE or Tibber.
Shelly and Tibber are trademarks of their owners.
"""
