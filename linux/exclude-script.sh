#!/bin/sh
# excludescript.sh
# Exclude one LAN device (e.g. a local AdGuard Home host) from forced DNS
# redirection (port 53 DNAT) on OpenWrt and GL.iNet routers.
#
# Usage:
#   sh excludescript.sh                      prompts for the IP
#   sh excludescript.sh 10.0.0.50            non-interactive
#   wget -qO- https://example.com/excludescript.sh | sh
#   wget -qO- https://example.com/excludescript.sh | sh -s -- 10.0.0.50
#
# fw3 (OpenWrt 21.02 and older, most GL.iNet 4.x firmware):
#   iptables rules applied by a firewall include that fw3 re-runs on every
#   firewall start/reload, inserted at the top of nat PREROUTING.
# fw4 (OpenWrt 22.03 and newer):
#   nftables rules placed at the start of the fw4 dstnat chain, ahead of the
#   per-zone jumps where port forwards and DNS redirects live.
#
# Excluded devices are kept in /etc/dns-bypass.list as "MAC IP", one per line.

set -u

LIST=/etc/dns-bypass.list
FW3_INCLUDE=/etc/dns-bypass.fw3
NFT_DIR=/usr/share/nftables.d/chain-pre/dstnat
NFT_FILE=$NFT_DIR/50-dns-bypass.nft

die() { echo "Error: $*" >&2; exit 1; }

valid_ip() {
    echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
    for octet in $(echo "$1" | tr '.' ' '); do
        [ "$octet" -le 255 ] || return 1
    done
}

valid_mac() {
    echo "$1" | grep -Eq '^([0-9a-f]{2}:){5}[0-9a-f]{2}$'
}

normalize_mac() {
    printf '%s' "$1" | tr 'A-F' 'a-f' | tr '-' ':'
}

# Read from the terminal so prompts still work when piped from wget/curl
prompt() {
    printf '%s\n' "$1"
    ANSWER=
    read -r ANSWER 2>/dev/null </dev/tty
}

lookup_mac() {
    # One ping forces ARP resolution if the neighbour entry has aged out.
    # The device does not need to answer ICMP, only ARP.
    ping -c 1 -W 1 "$1" >/dev/null 2>&1
    found=$(awk -v ip="$1" '$1 == ip && $4 != "00:00:00:00:00:00" { print $4; exit }' /proc/net/arp)
    if [ -z "$found" ] && [ -f /tmp/dhcp.leases ]; then
        found=$(awk -v ip="$1" '$3 == ip { print $2; exit }' /tmp/dhcp.leases)
    fi
    normalize_mac "$found"
}

keep_on_upgrade() {
    grep -qxF "$1" /etc/sysupgrade.conf 2>/dev/null || echo "$1" >> /etc/sysupgrade.conf
}

setup_fw3() {
    cat > "$FW3_INCLUDE" <<'EOF'
# Managed by excludescript.sh. fw3 runs this on every firewall start/reload.
# Inserts ACCEPT rules at the top of nat PREROUTING so listed MACs skip any
# port 53 DNAT. No "exit" in here because fw3 may source include scripts.
if [ -s /etc/dns-bypass.list ]; then
    _dnsb_rule() {
        for _dnsb_p in udp tcp; do
            # Delete old copies first so the rule always ends up at the very top
            while "$1" -t nat -D PREROUTING -m mac --mac-source "$2" -p "$_dnsb_p" \
                --dport 53 -m comment --comment dns-bypass -j ACCEPT 2>/dev/null; do :; done
            "$1" -t nat -I PREROUTING -m mac --mac-source "$2" -p "$_dnsb_p" \
                --dport 53 -m comment --comment dns-bypass -j ACCEPT
        done
    }
    _dnsb_v6=0
    ip6tables -t nat -nL PREROUTING >/dev/null 2>&1 && _dnsb_v6=1
    grep -Ev '^[[:space:]]*(#|$)' /etc/dns-bypass.list | while read -r _dnsb_mac _; do
        _dnsb_rule iptables "$_dnsb_mac"
        [ "$_dnsb_v6" = 1 ] && _dnsb_rule ip6tables "$_dnsb_mac"
    done
fi
EOF
    chmod 755 "$FW3_INCLUDE"

    if [ "$(uci -q get firewall.dns_bypass)" != "include" ]; then
        uci -q delete firewall.dns_bypass
        uci set firewall.dns_bypass=include
        uci set firewall.dns_bypass.type='script'
        uci set firewall.dns_bypass.path="$FW3_INCLUDE"
        uci set firewall.dns_bypass.reload='1'
        uci commit firewall
    fi

    /etc/init.d/firewall restart >/dev/null 2>&1 </dev/null

    iptables -t nat -C PREROUTING -m mac --mac-source "$MAC" -p udp --dport 53 \
        -m comment --comment dns-bypass -j ACCEPT 2>/dev/null
}

setup_fw4() {
    mkdir -p "$NFT_DIR"
    rules=$(mktemp)
    check=$(mktemp)

    {
        echo "# Managed by excludescript.sh, generated from $LIST"
        grep -Ev '^[[:space:]]*(#|$)' "$LIST" | while read -r m _; do
            printf 'ether saddr %s meta l4proto { tcp, udp } th dport 53 counter accept comment "dns-bypass"\n' "$m"
        done
    } > "$rules"

    # Validate in a throwaway table first. -c checks without committing anything.
    {
        echo 'table inet dns_bypass_check {'
        echo 'chain c { type nat hook prerouting priority dstnat; policy accept;'
        cat "$rules"
        echo '}'
        echo '}'
    } > "$check"
    if ! nft -c -f "$check" >/dev/null; then
        rm -f "$rules" "$check"
        die "generated nftables rules failed validation, firewall left untouched"
    fi
    rm -f "$check"
    mv "$rules" "$NFT_FILE"
    chmod 644 "$NFT_FILE"

    # fw4 reload is atomic: if the new ruleset fails to load, the old one stays
    /etc/init.d/firewall reload >/dev/null 2>&1 </dev/null

    nft list chain inet fw4 dstnat 2>/dev/null | grep -q "$MAC"
}

main() {
    [ "$(id -u)" -eq 0 ] || die "run this as root"

    if [ -x /sbin/fw4 ]; then
        FW=fw4
    elif [ -x /sbin/fw3 ]; then
        FW=fw3
    else
        die "neither fw3 nor fw4 found, this does not look like OpenWrt"
    fi

    IP=${1:-}
    if [ -z "$IP" ]; then
        prompt "What is the IP of the device that needs excluded:" ||
            die "no terminal to prompt on, pass the IP as an argument instead"
        IP=$ANSWER
    fi
    valid_ip "$IP" || die "'$IP' is not a valid IPv4 address"

    MAC=$(lookup_mac "$IP")
    if [ -z "$MAC" ]; then
        echo "No MAC found for $IP in the ARP table or DHCP leases."
        prompt "Enter the MAC manually (blank to abort):" || ANSWER=
        [ -n "$ANSWER" ] || die "aborted"
        MAC=$(normalize_mac "$ANSWER")
    fi
    valid_mac "$MAC" || die "'$MAC' is not a valid MAC address"
    echo "Using MAC $MAC for $IP"

    touch "$LIST"
    if awk -v m="$MAC" '$1 == m { f = 1 } END { exit !f }' "$LIST"; then
        echo "$MAC is already listed, reapplying rules."
    else
        echo "$MAC $IP" >> "$LIST"
    fi
    keep_on_upgrade "$LIST"

    if [ "$FW" = fw4 ]; then
        keep_on_upgrade "$NFT_FILE"
        setup_fw4 || die "bypass rule not found in the fw4 dstnat chain after reload, check 'logread -e firewall'"
    else
        keep_on_upgrade "$FW3_INCLUDE"
        setup_fw3 || die "bypass rule not found in nat PREROUTING after restart, check 'logread -e firewall'"
    fi

    echo "IP $IP excluded from port 53 re-write."
}

# Wrapped in main so the whole script is downloaded before anything runs when piped
main "$@"
