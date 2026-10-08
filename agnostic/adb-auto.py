#!/usr/bin/env python3
"""
adb-auto.py: find an Android device's Wireless Debugging port via mDNS and
run `adb connect` against it.

If the advertised address belongs to the machine running this script (e.g.
Termux on the phone itself), it connects via localhost. Otherwise it connects
to the advertised LAN address.

Exit codes:
    0    connected (or already connected)
    1    the 'zeroconf' library is not installed
    2    nothing found within the scan timeout
    3    'adb' was not found on PATH
    4    'adb connect' failed or timed out
    130  interrupted
"""

import ipaddress
import socket
import subprocess
import sys
import threading

# Attempt to load the zeroconf module, prompt user if missing
try:
    from zeroconf import IPVersion, ServiceBrowser, Zeroconf
except ImportError:
    print("Error: The 'zeroconf' library is missing.", file=sys.stderr)
    print("Install it by running: pip install zeroconf", file=sys.stderr)
    sys.exit(1)

# Android Wireless Debugging broadcasts on this specific service type
SERVICE_TYPE = "_adb-tls-connect._tcp.local."
SCAN_TIMEOUT = 5      # seconds to wait for a broadcast
CONNECT_TIMEOUT = 15  # seconds to wait for `adb connect`

EXIT_OK, EXIT_NO_ZEROCONF, EXIT_NOT_FOUND, EXIT_NO_ADB, EXIT_CONNECT_FAILED = 0, 1, 2, 3, 4


# Listener class to handle discovered mDNS services
class ADBListener:
    def __init__(self):
        self.found = threading.Event()
        self.port = None
        self.addresses = []
        self._lock = threading.Lock()

    # Required methods for ServiceBrowser, but unused here
    def remove_service(self, zeroconf, type, name):
        pass

    def update_service(self, zeroconf, type, name):
        pass

    # Triggered when a new mDNS service matches our target. Only the first
    # usable service is kept.
    def add_service(self, zeroconf, type, name):
        info = zeroconf.get_service_info(type, name)
        if not info or not info.port:
            return
        # Prefer IPv4: simpler for adb, and link-local IPv6 needs a scope id
        addresses = info.parsed_addresses(IPVersion.V4Only) or info.parsed_addresses()
        if not addresses:
            return
        with self._lock:
            if not self.found.is_set():
                self.port = info.port
                self.addresses = addresses
                self.found.set()


def is_local_address(address):
    """True if the address belongs to this machine.

    Loopback is local by definition. For anything else, try binding a UDP
    socket to it: that only succeeds if the address is assigned to one of this
    machine's interfaces. Standard library only, no packets are sent.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((address, 0))
        return True
    except OSError:
        return False


def format_target(host, port):
    # IPv6 literals need brackets for adb
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def connect(target):
    """Run `adb connect` and return an exit code.

    `adb connect` often exits 0 even when the connection fails, so the output
    is checked as well: both "connected to ..." and "already connected to ..."
    count as success.
    """
    try:
        result = subprocess.run(
            ["adb", "connect", target],
            capture_output=True, text=True, timeout=CONNECT_TIMEOUT,
        )
    except FileNotFoundError:
        print("Error: 'adb' was not found on PATH.", file=sys.stderr)
        return EXIT_NO_ADB
    except subprocess.TimeoutExpired:
        print(f"Error: 'adb connect {target}' timed out after {CONNECT_TIMEOUT}s.", file=sys.stderr)
        return EXIT_CONNECT_FAILED

    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)

    if result.returncode == 0 and "connected to" in output.lower():
        return EXIT_OK
    print("Error: adb did not report a successful connection.", file=sys.stderr)
    return EXIT_CONNECT_FAILED


def main():
    print("Scanning local network for Wireless Debugging broadcast...")

    zeroconf = Zeroconf()
    try:
        listener = ADBListener()
        ServiceBrowser(zeroconf, SERVICE_TYPE, listener)
        if not listener.found.wait(SCAN_TIMEOUT):
            print("Timeout. Ensure Wireless Debugging is enabled and you are connected to Wi-Fi.",
                  file=sys.stderr)
            return EXIT_NOT_FOUND
    finally:
        # Cleanup the network listener
        zeroconf.close()

    port, addresses = listener.port, listener.addresses
    print(f"Success! Found ADB port: {port} (advertised at {', '.join(addresses)})")

    # Same machine -> localhost; another device -> its advertised LAN address
    if any(is_local_address(a) for a in addresses):
        host = "localhost"
        print("Advertised address is this device, connecting via localhost.")
    else:
        host = addresses[0]
        print(f"Advertised address is another device, connecting to {host}.")

    return connect(format_target(host, port))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
