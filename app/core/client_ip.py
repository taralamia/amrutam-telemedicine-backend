import ipaddress

from starlette.requests import Request


def client_ip(request: Request, trusted_hops: int) -> str:
    peer = request.client.host if request.client is not None else "unknown"
    if trusted_hops == 0:
        return peer

    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for is None:
        return peer

    entries = [entry.strip() for entry in forwarded_for.split(",")]
    if len(entries) < trusted_hops or any(not entry for entry in entries):
        return peer

    try:
        addresses = [ipaddress.ip_address(entry) for entry in entries]
    except ValueError:
        return peer

    return str(addresses[-trusted_hops])
