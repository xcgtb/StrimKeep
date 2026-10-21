"""Address policy for the fixed, certificate-verified Telegram preview host."""
import ipaddress


def telegram_address(value):
    address = ipaddress.ip_address(value)
    if address.is_global:
        return 'public'
    # Clash/Mihomo's IPv4 fake-IP pool is a synthetic route through a TUN.
    # This exception is ONLY for fixed t.me HTTPS with TLS verification and no redirects.
    if address.version == 4 and address in ipaddress.ip_network('198.18.0.0/15'):
        return 'fake_ip'
    return 'blocked'
