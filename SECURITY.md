# Security policy

There are three kinds of security problem you might find. They go to
different places:

| You found a problem in… | Report it to | Section |
| --- | --- | --- |
| **This project**: the service's code, its CI, the container image or the dashboard | This repository, privately | [Reporting a vulnerability in this project](#reporting-a-vulnerability-in-this-project) |
| **Shelly's devices or services**: the firmware, the local API, the Shelly app or Shelly Cloud | Shelly. **Not** this repository. | [Vulnerabilities in Shelly's devices](#vulnerabilities-in-shellys-devices) |
| **Tibber's services**: the Tibber API or app | Tibber. **Not** this repository. | [Vulnerabilities in Tibber's services](#vulnerabilities-in-tibbers-services) |

This is an independent, unofficial hobby project. It isn't made, endorsed or
supported by Shelly Group SE or by Tibber, and it can't fix, and doesn't speak
for, anything on their side.

## Never expose it outside the tailnet

The service has **no login**. Whoever can open its page can change the rules
and switch the relay, and with it a boiler of about 1.1 kW. Run it where the
network is the gate, such as a Tailscale tailnet with no public route, and
never behind a public ingress, a port forward or a tunnel. That is the design,
not a vulnerability; see [Known and accepted](#known-and-accepted-not-vulnerabilities).

## Reporting a vulnerability in this project

**Don't open a public issue with the details.** Report it privately through
GitHub: **Security → Report a vulnerability** on this repository
([direct link](https://github.com/Yornik/Shelly-control/security/advisories/new)).

If that button is missing, open an issue that only asks for a private way to
report a security problem, with no details of it, and wait for an answer.

Please include:

- what's affected (file, version or image tag);
- how to reproduce it, ideally against the fake device and fake exporter in
  `tests/` rather than real ones;
- what an attacker could do with it.

**Never include real passwords, home ids, device ids, MAC addresses or IP
addresses**, not even your own. Redact them as `<redacted>`.

This is maintained in spare time, so handling is best effort:

- **Acknowledgement:** within 7 days.
- **Fix:** shipped as a new release, as soon as one is ready.
- **Disclosure:** coordinated with you, and a GitHub security advisory credits
  you unless you'd rather not be named.

### What's in scope

The things that matter most, given that the service switches mains power:

- **Switching around the allowlist.** Any way to make the service send a
  request to the device outside `ALLOWED_METHODS` (`Shelly.GetDeviceInfo`,
  `Switch.GetStatus`, `Switch.Set`), with a parameter it doesn't allow, to a
  path other than `/rpc/<method>` or to another host; any way to reach
  `set_output` other than through the controller's decision; or a way past the
  CI guard (`scripts/check_allowlist.py`). This is the most important class.
- **Switching when it shouldn't.** Any input (prices, a reply from the device,
  the state file, the clock) that makes the service switch in shadow mode
  except for an override, without `ALLOW_SWITCHING`, on a rule's decision
  without a usable price, or twice within `MIN_SWITCH_INTERVAL_SECONDS` (60 s
  by default, never below 10 s). An override switching without a price is by
  design: it needs none.
- **Getting past the page's checks.** A change accepted from another site or
  origin, without the token, by a method other than POST, PUT or DELETE, or
  under a host not in `ALLOWED_HOSTS` (DNS rebinding).
- **Cross-site scripting** or any markup injection into the page, for example
  through a price, a reason, the device's reply or the `Tailscale-User-Login`
  header.
- **Password exposure.** `SHELLY_PASSWORD` ending up in logs, metrics, the
  page, the API, error messages, the state files, the image or CI output.
- **Tampering with the state files** in a way that makes the service do more
  than the documented settings allow.
- **Supply chain:** the image, the GitHub Actions workflows, dependency
  pinning, or the release process.

### Supported versions

Only the latest release gets security fixes. Run the newest
`ghcr.io/yornik/shelly-control` tag.

### Known and accepted, not vulnerabilities

- **No login; the tailnet is the gate.** Anyone on the tailnet, and any pod in
  a cluster that doesn't enforce NetworkPolicy, can use the page and the API.
- **`/metrics`, `/healthz` and `/readyz` have no authentication** and no Host
  check, like every Prometheus exporter and probe endpoint: probes and scrapes
  arrive with the pod's IP as the Host. So a DNS-rebinding page in a browser on
  the tailnet could read `/metrics`: the settings, the mode, the relay state
  and the override. It holds no password, home id or token, and changes
  nothing.
- **`Tailscale-User-Login` is shown, not trusted.** The log's "by …" and the
  Status line "as named by the Tailscale proxy" are for information only. Any
  pod can reach the Service directly (Flannel enforces no NetworkPolicy) with
  any Host and that header, so the name can be forged.
- **`DEFAULT_MODE=live` starts the service live** without the confirmation the
  page and the API ask for. Whoever sets the deployment's environment already
  controls the service.
- **The device serves HTTP only.** The Plus 1PM's firmware (1.7.x) has no
  HTTPS. Digest authentication keeps the password itself off the wire, but
  requests and replies are readable on the network.
- **The device password is powerful by design.** Gen2 has one user, `admin`,
  and its password allows every change. This is how Shelly's authentication
  works.
- **A Gen2 device with authentication off can be switched by anyone on its
  network**, with or without this service. The page warns when it is off.

## Vulnerabilities in Shelly's devices

If you find a weakness in a **Shelly device, its firmware or local API, the
Shelly app or Shelly Cloud**, this project can't fix it. Please:

1. **Don't publish it**: not in an issue, pull request, discussion or commit
   here. A public write-up could let people switch devices that aren't theirs.
2. **Report it to Shelly** through their
   [security information and vulnerability reporting page](https://www.shelly.com/pages/security-information-and-vulnerability-reporting).
3. **Only test what you own.** Use your own device and your own account, and
   stop as soon as you can show the problem exists.

## Vulnerabilities in Tibber's services

This service never talks to Tibber; it reads prices from a tibber-exporter in
your own cluster. A weakness in the Tibber API or app belongs with Tibber, not
here. Don't publish it, and report it to Tibber directly.

## Disclaimer

This project and its maintainers aren't affiliated with Shelly Group SE or
with Tibber. **Shelly** and **Tibber** are trademarks of their owners. The
software is provided "as is", without warranty; see [LICENSE](LICENSE).
