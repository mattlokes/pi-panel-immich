import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("pi-panel-immich")


@dataclass
class ImmichClient:
    base_url: str
    api_key: str
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self):
        self.session.headers.update({"x-api-key": self.api_key, "Accept": "application/json"})
        self.base_url = self.base_url.rstrip("/")

    def get_random_assets(self, count: int = 50) -> list[dict]:
        try:
            r = self.session.get(f"{self.base_url}/api/assets/random", params={"count": count}, timeout=10)
            r.raise_for_status()
            return [a for a in r.json() if a.get("type") == "IMAGE"]
        except Exception as e:
            log.error(f"Failed to fetch random assets: {e}")
            return []

    def get_album_assets(self, album_id: str) -> list[dict]:
        try:
            r = self.session.get(f"{self.base_url}/api/albums/{album_id}", timeout=10)
            r.raise_for_status()
            return [a for a in r.json().get("assets", []) if a.get("type") == "IMAGE"]
        except Exception as e:
            log.error(f"Failed to fetch album assets: {e}")
            return []

    def download_thumbnail(self, asset_id: str, size: str = "preview") -> Optional[bytes]:
        try:
            r = self.session.get(f"{self.base_url}/api/assets/{asset_id}/thumbnail", params={"size": size}, timeout=15)
            r.raise_for_status()
            return r.content
        except Exception as e:
            log.error(f"Failed to download {asset_id}: {e}")
            return None
