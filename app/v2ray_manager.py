from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import time
import uuid

from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests


class V2RayError(RuntimeError):
    pass


class V2RayManager:

    def __init__(
        self,
        base_dir: str | Path = "v2ray_data",
    ):
        self.base_dir = Path(base_dir)

        self.base_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.saved_file = (
            self.base_dir / "v2ray_configs.json"
        )

        self.processes: dict[int, subprocess.Popen] = {}

        self.ports: dict[int, int] = {}

    # =========================================================
    # Save / Load
    # =========================================================

    def _load_saved(self) -> dict:

        if not self.saved_file.exists():
            return {}

        try:

            with self.saved_file.open(
                "r",
                encoding="utf-8",
            ) as f:

                data = json.load(f)

            if isinstance(data, dict):
                return data

        except Exception:
            pass

        return {}

    def _save_saved(
        self,
        data: dict,
    ) -> None:

        tmp = (
            self.saved_file.with_suffix(
                ".tmp"
            )
        )

        with tmp.open(
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

        tmp.replace(
            self.saved_file
        )

    def get_saved(
        self,
        user_id: int,
    ) -> str | None:

        data = self._load_saved()

        item = data.get(
            str(user_id)
        )

        if not isinstance(
            item,
            dict,
        ):
            return None

        value = item.get(
            "vless"
        )

        return (
            value
            if isinstance(value, str)
            else None
        )

    def save(
        self,
        user_id: int,
        vless: str,
        public_ip: str | None,
    ) -> None:

        data = self._load_saved()

        data[str(user_id)] = {
            "vless": vless,
            "public_ip": public_ip,
            "saved_at": int(time.time()),
        }

        self._save_saved(
            data
        )

    # =========================================================
    # VLESS parsing
    # =========================================================

    @staticmethod
    def _parse_vless(
        vless: str,
    ) -> dict:

        vless = vless.strip()

        if not vless.startswith(
            "vless://"
        ):

            raise V2RayError(
                "لینک باید با vless:// شروع شود."
            )

        parsed = urlparse(
            vless
        )

        if not parsed.username:

            raise V2RayError(
                "UUID در VLESS پیدا نشد."
            )

        if not parsed.hostname:

            raise V2RayError(
                "Server در VLESS پیدا نشد."
            )

        if not parsed.port:

            raise V2RayError(
                "Port در VLESS پیدا نشد."
            )

        query = parse_qs(
            parsed.query
        )

        network = (
            query.get(
                "type",
                ["tcp"],
            )[0]
        )

        security = (
            query.get(
                "security",
                ["none"],
            )[0]
        )

        if network != "ws":

            raise V2RayError(
                "فعلاً فقط VLESS + WebSocket پشتیبانی می‌شود."
            )

        if security != "tls":

            raise V2RayError(
                "فعلاً فقط VLESS + TLS + WebSocket پشتیبانی می‌شود."
            )

        sni = (
            query.get(
                "sni",
                [parsed.hostname],
            )[0]
        )

        host = (
            query.get(
                "host",
                [sni],
            )[0]
        )

        raw_path = (
            query.get(
                "path",
                ["/"],
            )[0]
        )

        ws_path = unquote(
            raw_path
        )

        uuid_value = (
            parsed.username
        )

        # Basic UUID validation
        uuid_pattern = re.compile(
            r"^[0-9a-fA-F]{8}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{12}$"
        )

        if not uuid_pattern.match(
            uuid_value
        ):

            raise V2RayError(
                "UUID معتبر نیست."
            )

        return {
            "uuid": uuid_value,
            "address": parsed.hostname,
            "port": parsed.port,
            "sni": sni,
            "host": host,
            "path": ws_path,
        }

    # =========================================================
    # Port
    # =========================================================

    @staticmethod
    def _find_free_port() -> int:

        with socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM,
        ) as sock:

            sock.bind(
                ("127.0.0.1", 0)
            )

            return sock.getsockname()[1]

    # =========================================================
    # Xray config
    # =========================================================

    def _create_config(
        self,
        parsed: dict,
        socks_port: int,
    ) -> Path:

        config = {

            "log": {
                "loglevel": "warning",
            },

            "inbounds": [
                {
                    "listen": "127.0.0.1",
                    "port": socks_port,
                    "protocol": "socks",
                    "settings": {
                        "auth": "noauth",
                        "udp": True,
                    },
                }
            ],

            "outbounds": [
                {
                    "protocol": "vless",

                    "settings": {
                        "vnext": [
                            {
                                "address":
                                    parsed["address"],

                                "port":
                                    parsed["port"],

                                "users": [
                                    {
                                        "id":
                                            parsed["uuid"],

                                        "encryption":
                                            "none",
                                    }
                                ],
                            }
                        ],
                    },

                    "streamSettings": {
                        "network": "ws",

                        "security": "tls",

                        "tlsSettings": {
                            "serverName":
                                parsed["sni"],

                            "allowInsecure":
                                False,

                            "fingerprint":
                                "chrome",
                        },

                        "wsSettings": {
                            "path":
                                parsed["path"],

                            "headers": {
                                "Host":
                                    parsed["host"],
                            },
                        },
                    },
                }
            ],
        }

        config_dir = Path(
            tempfile.mkdtemp(
                prefix="xray_",
                dir=self.base_dir,
            )
        )

        config_path = (
            config_dir / "config.json"
        )

        with config_path.open(
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                config,
                f,
                ensure_ascii=False,
                indent=2,
            )

        return config_path

    # =========================================================
    # Start Xray
    # =========================================================

    def _start(
        self,
        user_id: int,
        config_path: Path,
        port: int,
    ) -> None:

        xray_bin = os.getenv(
            "XRAY_BINARY",
            "xray",
        )

        process = subprocess.Popen(
            [
                xray_bin,
                "run",
                "-c",
                str(config_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        self.processes[user_id] = process
        self.ports[user_id] = port

        # Wait until SOCKS starts listening
        deadline = (
            time.time() + 10
        )

        while time.time() < deadline:

            if process.poll() is not None:

                stderr = ""

                try:
                    stderr = (
                        process.stderr.read()
                        .decode(
                            "utf-8",
                            errors="replace",
                        )
                    )

                except Exception:
                    pass

                raise V2RayError(
                    "Xray اجرا نشد.\n"
                    + stderr[-1000:]
                )

            try:

                with socket.create_connection(
                    (
                        "127.0.0.1",
                        port,
                    ),
                    timeout=0.5,
                ):
                    return

            except OSError:
                time.sleep(0.2)

        raise V2RayError(
            "Xray در زمان مقرر آماده نشد."
        )

    # =========================================================
    # Stop
    # =========================================================

    def stop(
        self,
        user_id: int,
    ) -> None:

        process = self.processes.pop(
            user_id,
            None,
        )

        self.ports.pop(
            user_id,
            None,
        )

        if process is None:
            return

        try:

            process.terminate()
            process.wait(
                timeout=3
            )

        except Exception:

            try:
                process.kill()
            except Exception:
                pass

    # =========================================================
    # Get requests session
    # =========================================================

    def get_requests_session(
        self,
        user_id: int,
    ) -> requests.Session:

        port = self.ports.get(
            user_id
        )

        if not port:

            raise V2RayError(
                "V2Ray برای این کاربر فعال نیست."
            )

        session = requests.Session()
        session.trust_env = False

        proxy = (
            f"socks5h://127.0.0.1:{port}"
        )

        session.proxies.update(
            {
                "http": proxy,
                "https": proxy,
            }
        )

        return session

    def create_proxy_session(
        self,
        user_id: int,
    ) -> requests.Session:
        """Create a SOCKS5 requests session for owner-ID lookup only."""
        return self.get_requests_session(user_id)

    # =========================================================
    # Test Instagram
    # =========================================================

    def test_instagram(
        self,
        user_id: int,
        instagram_username: str,
    ) -> tuple[
        bool,
        str | None,
        str | None,
    ]:

        session = (
            self.get_requests_session(
                user_id
            )
        )

        # -----------------------------
        # IP test
        # -----------------------------

        try:

            ip_response = session.get(
                "https://api.ipify.org?format=text",
                timeout=15,
            )

            if ip_response.status_code != 200:

                return (
                    False,
                    None,
                    "تست IP ناموفق بود.",
                )

            public_ip = (
                ip_response.text.strip()
            )

        except Exception as exc:

            return (
                False,
                None,
                f"خطا در تست Proxy: {exc}",
            )

        # -----------------------------
        # Instagram profile HTML
        # -----------------------------

        profile_url = (
            f"https://www.instagram.com/"
            f"{instagram_username}/"
        )

        headers = {
            "User-Agent":
                "Mozilla/5.0",

            "X-IG-App-ID":
                "936619743392459",

            "Referer":
                "https://www.instagram.com/",

            "Accept":
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8",
        }

        try:

            response = session.get(
                profile_url,
                headers=headers,
                timeout=20,
            )

        except Exception as exc:

            return (
                False,
                public_ip,
                f"Instagram connection failed: {exc}",
            )

        if response.status_code != 200:

            return (
                False,
                public_ip,
                (
                    "Instagram profile test failed: "
                    f"HTTP {response.status_code}"
                ),
            )

        html = response.text

        patterns = [
            r'"profile_id":"(\d+)"',

            r'"user_id":"(\d+)"',

            r'"owner":\{"id":"(\d+)"',

            r'"id":"(\d+)","username":"' +
            re.escape(
                instagram_username
            ),
        ]

        owner_id = None

        for pattern in patterns:

            match = re.search(
                pattern,
                html,
            )

            if match:

                owner_id = match.group(1)

                break

        if owner_id is None:

            return (
                False,
                public_ip,
                "Instagram باز شد ولی User ID پیدا نشد.",
            )

        return (
            True,
            public_ip,
            owner_id,
        )

    # =========================================================
    # Validate and save
    # =========================================================

    def validate_and_save(
        self,
        user_id: int,
        vless: str,
        instagram_username: str,
    ) -> tuple[
        bool,
        str,
        str | None,
    ]:

        parsed = self._parse_vless(
            vless
        )

        self.stop(
            user_id
        )

        socks_port = (
            self._find_free_port()
        )

        config_path = self._create_config(
            parsed,
            socks_port,
        )

        try:

            self._start(
                user_id,
                config_path,
                socks_port,
            )

            ok, public_ip, result = (
                self.test_instagram(
                    user_id,
                    instagram_username,
                )
            )

            if not ok:

                self.stop(
                    user_id
                )

                return (
                    False,
                    result or "Proxy نامعتبر است.",
                    public_ip,
                )

            self.save(
                user_id,
                vless,
                public_ip,
            )

            return (
                True,
                (
                    "Proxy سالم است.\n"
                    f"IP: {public_ip}\n"
                    f"Instagram User ID: {result}"
                ),
                public_ip,
            )

        except Exception:

            self.stop(
                user_id
            )

            raise
