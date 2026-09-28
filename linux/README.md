# linux/

Scripts here depend on Linux-only tools, paths, or package managers, and are not expected to run unmodified on Windows or macOS.

## Scripts

### `exclude-script.sh`

Exempts one LAN device, such as an AdGuard Home or Pi-hole host, from the forced DNS redirect on OpenWrt and GL.iNet routers so its own upstream lookups are not looped back through the router. Give it the device's IP and it looks up the MAC on the router, then adds port 53 bypass rules for that MAC.

- **Requirements:** An OpenWrt or GL.iNet router with a root shell (SSH), plus:
  - fw3 (OpenWrt 21.02 and older): `iptables` with the `mac` and `comment` matches, normally present on stock builds. IPv6 rules are only added when the ip6tables `nat` table exists.
  - fw4 (OpenWrt 22.03 and newer): `nft`, which fw4 already depends on.
- **Platform notes:** Runs on the router itself, not on a desktop distro. It relies on OpenWrt's `uci`, the fw3/fw4 firewall, `/proc/net/arp`, dnsmasq's `/tmp/dhcp.leases`, and `/etc/sysupgrade.conf`. Written for BusyBox `sh`, since OpenWrt does not ship bash by default. The script detects fw3 or fw4 and uses iptables or nftables accordingly.
- **Usage:** Run as root on the router. Stock OpenWrt has `wget` but no curl; GL.iNet firmware has both.
  ```sh
  wget -qO- https://raw.githubusercontent.com/MZGSZM/scripts/main/linux/exclude-script.sh | sh
  wget -qO- https://raw.githubusercontent.com/MZGSZM/scripts/main/linux/exclude-script.sh | sh -s -- 10.0.0.50
  sh exclude-script.sh [ip]
  ```
- **Key options:** An optional IPv4 address as the only argument. Without it, the script prompts for one. Running it again with another IP adds another device; rerunning with an already listed device reapplies the rules without duplicating them.
- **Interactive:** Prompts read from `/dev/tty`, so they work when the script is piped. If the MAC is not in the ARP table or DHCP leases, it asks for one manually.
- **Firewall changes:** Restarts the firewall on fw3 and reloads it on fw4. On fw4 the generated rules are validated with `nft -c` first and the reload is atomic, so a bad rule leaves the existing ruleset in place. On both, the script confirms the bypass rule is live before reporting success.
- **Files written:** `/etc/dns-bypass.list` (one `MAC IP` pair per line). On fw3, `/etc/dns-bypass.fw3` plus a `firewall.dns_bypass` UCI include that reapplies the rules on every firewall start and reload. On fw4, `/usr/share/nftables.d/chain-pre/dstnat/50-dns-bypass.nft`. The list and rule file are added to `/etc/sysupgrade.conf` so they survive firmware upgrades that keep settings.
- **Removal:** There is no remove flag. Delete the device's line from `/etc/dns-bypass.list`, then:
  - fw3: clear the old rules and restart the firewall so the remaining devices are reapplied.
    ```sh
    for t in iptables ip6tables; do
      $t -t nat -S PREROUTING 2>/dev/null | grep -- '--comment dns-bypass' | sed 's/^-A/-D/' |
        while read -r rule; do $t -t nat $rule; done
    done
    /etc/init.d/firewall restart
    ```
  - fw4: delete the matching line from `50-dns-bypass.nft`, then run `/etc/init.d/firewall reload`.
- **Existing manual rules:** If you previously added bypass rules to `/etc/firewall.user`, remove them once these are verified to avoid duplicate rules.
- **Verify:** `iptables -t nat -L PREROUTING -n -v` on fw3, or `nft list chain inet fw4 dstnat` on fw4. The `dns-bypass` rules should sit above any port 53 redirect, and their counters should climb when the DNS server makes plain DNS queries.
- **GL.iNet caveat:** The script checks that its own rule exists, not that it is ordered above GL.iNet's redirect. If toggling "Override DNS Settings for All Clients" reapplies the redirect without a firewall restart, it can land on top of the bypass. Run `/etc/init.d/firewall restart` after changing that setting and verify. On fw4-based GL.iNet firmware, a redirect implemented in its own nftables table would not be affected by this bypass.
- **fw4 on early 22.03:** Support for the `chain-pre` include directory is unconfirmed on the earliest 22.03 point releases. If fw4 ignores the file, the script reports that the rule was not found instead of claiming success.
- **Security tradeoff:** Any LAN client that spoofs the excluded MAC also skips the redirect. Matching on IP would have the same weakness.
- **Scope:** MAC matching only works for devices on the same layer 2 network as the router; a DNS server behind another router shows up with that router's MAC. The script only skips the port 53 DNAT, so a separate forward rule blocking port 53 or 853 to the WAN still needs its own exception.
- **Testing status:** Syntax-checked with BusyBox ash and shellcheck, the fw3 include tested against mocked iptables, and the generated nftables rules validated with `nft -c`. Not yet tested on live OpenWrt or GL.iNet hardware.

---
