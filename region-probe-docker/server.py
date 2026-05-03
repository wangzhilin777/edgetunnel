import json
import os
import re
import socket
import ssl
import time
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse


TLS_PORTS = {443, 2053, 2083, 2087, 2096, 8443}
PLAIN_PORTS = {80, 8080, 8880, 2052, 2082, 2086, 2095}
TRACE_HOST = os.getenv("TRACE_HOST", "speed.cloudflare.com")
TRACE_PATH = os.getenv("TRACE_PATH", "/cdn-cgi/trace")
LISTEN_HOST = os.getenv("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))
AUTH_TOKEN = os.getenv("REGION_PROBE_TOKEN", "").strip()
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "3.5"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "4.5"))
MAX_WORKERS = max(1, int(os.getenv("MAX_WORKERS", "24")))
MAX_TARGETS = max(1, int(os.getenv("MAX_TARGETS", "500")))
TLS_SERVER_NAME = os.getenv("TLS_SERVER_NAME", TRACE_HOST)
PROXY_URL = (
    os.getenv("PROXY_URL", "").strip()
    or os.getenv("PROXY_SOCKS5", "").strip()
    or os.getenv("ALL_PROXY", "").strip()
)
PROXY_REMOTE_DNS = os.getenv("PROXY_REMOTE_DNS", "false").strip().lower() in {"1", "true", "yes", "on"}

TRACE_REQUEST = (
    f"GET {TRACE_PATH} HTTP/1.1\r\n"
    f"Host: {TRACE_HOST}\r\n"
    "User-Agent: region-probe-docker/1.0\r\n"
    "Accept: text/plain,*/*\r\n"
    "Connection: close\r\n\r\n"
).encode("ascii")

COLO_PATTERN = re.compile(rb"(?:^|\n)colo=([A-Z0-9]{3,4})\b", re.I)
CF_RAY_PATTERN = re.compile(rb"(?:^|\n)CF-RAY:\s*[^-\r\n]+-([A-Z0-9]{3,4})\b", re.I)
COLO_TO_REGION = {
    "HKG": "HK", "MFM": "MO", "TPE": "TW", "KHH": "TW", "TSA": "TW",
    "NRT": "JP", "HND": "JP", "KIX": "JP", "CTS": "JP", "FUK": "JP", "OKA": "JP",
    "ICN": "KR", "GMP": "KR", "PUS": "KR",
    "SIN": "SG", "KUL": "MY", "BKI": "MY", "CGK": "ID", "SUB": "ID",
    "BKK": "TH", "MNL": "PH", "SGN": "VN", "HAN": "VN",
    "DEL": "IN", "BOM": "IN", "MAA": "IN", "BLR": "IN", "HYD": "IN", "CCU": "IN",
    "LAX": "US", "SJC": "US", "SEA": "US", "PDX": "US", "DEN": "US", "PHX": "US",
    "DFW": "US", "ORD": "US", "IAD": "US", "ATL": "US", "MIA": "US", "JFK": "US",
    "EWR": "US", "BOS": "US", "MSP": "US", "DTW": "US", "LAS": "US", "CLT": "US",
    "YYZ": "CA", "YVR": "CA", "YUL": "CA", "YYC": "CA", "YOW": "CA",
    "LHR": "GB", "LGW": "GB", "MAN": "GB", "GLA": "GB", "EDI": "GB", "DUB": "IE",
    "AMS": "NL", "RTM": "NL", "FRA": "DE", "MUC": "DE", "BER": "DE", "DUS": "DE", "HAM": "DE",
    "CDG": "FR", "MRS": "FR", "LYS": "FR", "MAD": "ES", "BCN": "ES", "LIS": "PT", "OPO": "PT",
    "MXP": "IT", "FCO": "IT", "VIE": "AT", "ZRH": "CH", "GVA": "CH", "BRU": "BE",
    "CPH": "DK", "ARN": "SE", "OSL": "NO", "HEL": "FI", "WAW": "PL", "PRG": "CZ", "BUD": "HU",
    "OTP": "RO", "SOF": "BG", "ATH": "GR", "IST": "TR",
    "DXB": "AE", "AUH": "AE", "DOH": "QA", "TLV": "IL", "AMM": "JO", "KWI": "KW",
    "JNB": "ZA", "CPT": "ZA", "NBO": "KE", "LOS": "NG", "CMN": "MA",
    "SYD": "AU", "MEL": "AU", "BNE": "AU", "PER": "AU", "AKL": "NZ",
}


def parse_proxy_config(proxy_url: str) -> Optional[Dict]:
    value = str(proxy_url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"socks5://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"socks5", "socks5h", "http", "https"}:
        raise ValueError(f"unsupported proxy scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("proxy host or port missing")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": parsed.username or "",
        "password": parsed.password or "",
        "remote_dns": PROXY_REMOTE_DNS or parsed.scheme == "socks5h",
    }


PROXY_CONFIG = parse_proxy_config(PROXY_URL) if PROXY_URL else None


def json_response(handler: BaseHTTPRequestHandler, payload: Dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def normalize_host(host: str) -> str:
    value = str(host or "").strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return value


def is_ip(host: str) -> bool:
    try:
        ip_address(host)
        return True
    except ValueError:
        return False


def resolve_host(host: str, port: int) -> List[Tuple[socket.AddressFamily, str]]:
    if not host:
        return []
    if is_ip(host):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        return [(family, host)]
    resolved: List[Tuple[socket.AddressFamily, str]] = []
    seen = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = sockaddr[0]
        key = (family, ip)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(key)
    resolved.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    return resolved


def recv_trace(sock: socket.socket) -> bytes:
    sock.settimeout(READ_TIMEOUT)
    chunks: List[bytes] = []
    total = 0
    while total < 65536:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        data = b"".join(chunks)
        if b"\ncolo=" in data or b"\nloc=" in data:
            return data
    return b"".join(chunks)


def recv_until(sock: socket.socket, marker: bytes, max_bytes: int = 65536) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while total < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        data = b"".join(chunks)
        if marker in data:
            return data
    return b"".join(chunks)


def is_ipv4(host: str) -> bool:
    return "." in host and is_ip(host)


def build_socks5_address(host: str, family: socket.AddressFamily) -> bytes:
    if is_ipv4(host):
        return b"\x01" + socket.inet_aton(host)
    if family == socket.AF_INET6 and is_ip(host):
        return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    host_bytes = host.encode("idna")
    if len(host_bytes) > 255:
        raise ValueError("proxy target host too long")
    return b"\x03" + bytes([len(host_bytes)]) + host_bytes


def socks5_handshake(sock: socket.socket, target_host: str, target_port: int, family: socket.AddressFamily) -> None:
    username = PROXY_CONFIG["username"]
    password = PROXY_CONFIG["password"]
    methods = [0x00] if not username else [0x00, 0x02]
    sock.sendall(bytes([0x05, len(methods), *methods]))
    greeting = sock.recv(2)
    if len(greeting) < 2 or greeting[0] != 0x05 or greeting[1] == 0xFF:
        raise RuntimeError("SOCKS5 greeting failed")
    if greeting[1] == 0x02:
        user_bytes = username.encode("utf-8")
        pass_bytes = password.encode("utf-8")
        auth_req = b"\x01" + bytes([len(user_bytes)]) + user_bytes + bytes([len(pass_bytes)]) + pass_bytes
        sock.sendall(auth_req)
        auth_resp = sock.recv(2)
        if len(auth_resp) < 2 or auth_resp[1] != 0x00:
            raise RuntimeError("SOCKS5 auth failed")
    connect_host = target_host if PROXY_CONFIG.get("remote_dns") else target_host
    addr = build_socks5_address(connect_host, family)
    request = b"\x05\x01\x00" + addr + int(target_port).to_bytes(2, "big")
    sock.sendall(request)
    header = sock.recv(4)
    if len(header) < 4 or header[1] != 0x00:
        raise RuntimeError(f"SOCKS5 connect failed: {header[1] if len(header) > 1 else 'short'}")
    atyp = header[3]
    if atyp == 0x01:
        sock.recv(4)
    elif atyp == 0x03:
        length = sock.recv(1)[0]
        sock.recv(length)
    elif atyp == 0x04:
        sock.recv(16)
    sock.recv(2)


def http_connect_handshake(sock: socket.socket, target_host: str, target_port: int) -> None:
    connect_host = target_host
    headers = [f"CONNECT {connect_host}:{target_port} HTTP/1.1", f"Host: {connect_host}:{target_port}"]
    username = PROXY_CONFIG["username"]
    password = PROXY_CONFIG["password"]
    if username:
        token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers.append(f"Proxy-Authorization: Basic {token}")
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
    sock.sendall(request)
    response = recv_until(sock, b"\r\n\r\n")
    first_line = response.split(b"\r\n", 1)[0]
    if b" 200 " not in first_line:
        raise RuntimeError(f"HTTP CONNECT failed: {first_line.decode('latin1', 'ignore')}")


def create_transport_socket(target_host: str, target_port: int, family: socket.AddressFamily) -> socket.socket:
    if not PROXY_CONFIG:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT)
        sock.connect((target_host, target_port))
        return sock

    proxy_family = socket.AF_INET6 if ":" in PROXY_CONFIG["host"] and not is_ipv4(PROXY_CONFIG["host"]) else socket.AF_INET
    sock = socket.socket(proxy_family, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT)
    sock.connect((PROXY_CONFIG["host"], PROXY_CONFIG["port"]))
    if PROXY_CONFIG["scheme"] in {"socks5", "socks5h"}:
        socks5_handshake(sock, target_host, target_port, family)
    else:
        http_connect_handshake(sock, target_host, target_port)
    return sock


def parse_trace_result(data: bytes) -> Tuple[Optional[str], Optional[str]]:
    colo_match = COLO_PATTERN.search(data)
    ray_match = CF_RAY_PATTERN.search(data)
    colo = colo_match.group(1).decode("ascii").upper() if colo_match else None
    ray_colo = ray_match.group(1).decode("ascii").upper() if ray_match else None
    effective_colo = colo or ray_colo
    region = COLO_TO_REGION.get(effective_colo or "", None) if effective_colo else None
    return effective_colo, region


def detect_via_plain(ip: str, port: int, family: socket.AddressFamily) -> Tuple[Optional[str], Optional[str]]:
    with create_transport_socket(ip, port, family) as sock:
        sock.sendall(TRACE_REQUEST)
        data = recv_trace(sock)
    return parse_trace_result(data)


def detect_via_tls(ip: str, port: int, family: socket.AddressFamily) -> Tuple[Optional[str], Optional[str]]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with create_transport_socket(ip, port, family) as raw_sock:
        with context.wrap_socket(raw_sock, server_hostname=TLS_SERVER_NAME) as tls_sock:
            tls_sock.settimeout(READ_TIMEOUT)
            tls_sock.sendall(TRACE_REQUEST)
            data = recv_trace(tls_sock)
    return parse_trace_result(data)


def detect_region_for_target(target: Dict) -> Dict:
    host = normalize_host(target.get("host", ""))
    port = int(target.get("port") or 443)
    remark = str(target.get("remark", "") or "")
    result = {
        "host": host,
        "port": port,
        "remark": remark,
        "region": None,
        "colo": None,
        "ip": None,
        "source": "trace-proxy" if PROXY_CONFIG else "trace",
        "latency_ms": None,
        "error": None,
    }
    if not host:
        result["error"] = "missing host"
        return result

    families = resolve_host(host, port)
    if not families:
        result["error"] = "resolve failed"
        return result

    if port in PLAIN_PORTS:
        modes = ("plain",)
    elif port in TLS_PORTS:
        modes = ("tls",)
    else:
        modes = ("tls", "plain")

    started = time.perf_counter()
    errors: List[str] = []
    for family, ip in families:
        for mode in modes:
            try:
                colo, region = detect_via_tls(ip, port, family) if mode == "tls" else detect_via_plain(ip, port, family)
                if region:
                    result["colo"] = colo
                    result["region"] = region
                    result["ip"] = ip
                    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
                    return result
            except Exception as exc:
                errors.append(f"{mode}:{ip}:{exc}")

    result["error"] = " | ".join(errors[:4]) if errors else "trace failed"
    return result


class RegionProbeHandler(BaseHTTPRequestHandler):
    server_version = "region-probe-docker/1.0"

    def do_GET(self) -> None:
        if self.path in ("/", "/health", "/healthz", "/ready"):
            json_response(self, {
                "success": True,
                "service": "region-probe-docker",
                "trace_host": TRACE_HOST,
                "listen": f"{LISTEN_HOST}:{LISTEN_PORT}",
                "proxy": PROXY_CONFIG["scheme"] + "://" + PROXY_CONFIG["host"] + f':{PROXY_CONFIG["port"]}' if PROXY_CONFIG else None,
                "trace_result": "colo->region (fallback cf-ray)",
            })
            return
        json_response(self, {"success": False, "message": "not found"}, 404)

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in ("/region-probe", "/probe"):
            json_response(self, {"success": False, "message": "not found"}, 404)
            return
        if AUTH_TOKEN:
            auth = self.headers.get("Authorization", "")
            header_token = self.headers.get("X-Region-Probe-Token", "")
            bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if AUTH_TOKEN not in (header_token, bearer):
                json_response(self, {"success": False, "message": "unauthorized"}, 401)
                return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length <= 0:
            json_response(self, {"success": False, "message": "empty body"}, 400)
            return

        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except Exception:
            json_response(self, {"success": False, "message": "invalid json"}, 400)
            return

        targets = payload.get("targets")
        if not isinstance(targets, list):
            json_response(self, {"success": False, "message": "targets must be array"}, 400)
            return
        if len(targets) > MAX_TARGETS:
            json_response(self, {"success": False, "message": f"targets too many, max {MAX_TARGETS}"}, 400)
            return

        results: List[Dict] = []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(targets)))) as executor:
            futures = [executor.submit(detect_region_for_target, target if isinstance(target, dict) else {}) for target in targets]
            for future in as_completed(futures):
                results.append(future.result())

        result_map = {
            f'{item["host"]}:{item["port"]}': item["region"]
            for item in results
            if item.get("host") and item.get("region")
        }
        success_count = sum(1 for item in results if item.get("region"))
        json_response(self, {
            "success": True,
            "service": "region-probe-docker",
            "success_count": success_count,
            "total": len(targets),
            "data": results,
            "map": result_map,
        })

    def log_message(self, format: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {self.address_string()} {format % args}")


if __name__ == "__main__":
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), RegionProbeHandler)
    proxy_text = f", proxy={PROXY_CONFIG['scheme']}://{PROXY_CONFIG['host']}:{PROXY_CONFIG['port']}" if PROXY_CONFIG else ""
    print(f"region-probe-docker listening on {LISTEN_HOST}:{LISTEN_PORT}, trace host={TRACE_HOST}{proxy_text}")
    server.serve_forever()
