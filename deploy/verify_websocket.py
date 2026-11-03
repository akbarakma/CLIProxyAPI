import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import time

import yaml


ROOT = Path(__file__).resolve().parent.parent


def send(connection, opcode, body):
    mask = os.urandom(4)
    length = len(body)
    header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else bytes([0x80 | opcode, 0xFE]) + struct.pack("!H", length)
    connection.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(body)))


def receive(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise RuntimeError("Websocket closed before the response completed")
    return data


def main():
    key = yaml.safe_load((ROOT / "config.yaml").read_text())["access"]["api-keys"][0]
    log = ROOT / "logs/main.log"
    offset = log.stat().st_size
    challenge = base64.b64encode(os.urandom(16)).decode()
    deadline = time.monotonic() + 60
    with socket.create_connection(("127.0.0.1", 8317), timeout=15) as connection:
        connection.sendall(("GET /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:8317\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                            "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + challenge + "\r\n"
                            "Authorization: Bearer " + key + "\r\nUser-Agent: codex_cli_rs\r\n\r\n").encode())
        with connection.makefile("rb") as stream:
            assert b" 101 " in stream.readline()
            headers = {}
            while True:
                line = stream.readline()
                if line == b"\r\n":
                    break
                if not line:
                    raise RuntimeError("Incomplete websocket handshake")
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((challenge + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            assert headers["sec-websocket-accept"] == accept
            send(connection, 1, json.dumps({
                "type": "response.create", "model": "gpt-6-luna", "store": False,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with exactly OK."}]}],
                "reasoning": {"effort": "none"},
            }).encode())
            message = b""
            while time.monotonic() < deadline:
                connection.settimeout(max(0.1, deadline - time.monotonic()))
                first, second = receive(stream, 2)
                assert not second & 0x80
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", receive(stream, 2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", receive(stream, 8))[0]
                assert length <= 1_048_576
                payload = receive(stream, length)
                opcode = first & 0x0F
                if opcode == 9:
                    send(connection, 10, payload)
                    continue
                if opcode == 8:
                    raise RuntimeError("Websocket closed before completion")
                if opcode not in [0, 1]:
                    continue
                message += payload
                if not first & 0x80:
                    continue
                event = json.loads(message)
                message = b""
                if event.get("type") == "error":
                    raise RuntimeError("The websocket returned an API error")
                if event.get("type") == "response.completed":
                    assert event["response"]["status"] == "completed"
                    assert "OK" in json.dumps(event["response"].get("output", []))
                    send(connection, 8, struct.pack("!H", 1000))
                    break
            else:
                raise RuntimeError("Websocket response exceeded 60 seconds")
    with log.open() as handle:
        handle.seek(offset)
        lines = handle.read()
    assert "codex websockets: upstream connected" in lines
    print("PASS: client websocket, completed Codex response, and upstream websocket connection")


if __name__ == "__main__":
    main()
