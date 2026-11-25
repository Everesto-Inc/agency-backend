"""Tiny S3-compatible client for OCI Object Storage (put / get / delete), signed with AWS Signature V4.

    store = ObjectStore.from_settings(settings, http)      # None when S3_* isn't configured
    await store.put("web/<id>", data, "image/jpeg")         # → <bucket>/<prefix>web/<id>
    data, content_type = await store.get("web/<id>")

Path-style addressing (https://<namespace>.compat.objectstorage.<region>.oraclecloud.com/<bucket>/<key>), which
is what OCI's S3 compatibility API expects. No SDK dependency: three calls don't justify boto3.
The bucket has versioning disabled (oracle_infra 03_storage), so delete really deletes.
"""
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

import httpx

from agency_platform.errors import NotFoundError, UpstreamError

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_v4(*, method: str, url: str, headers: dict[str, str], payload_sha256: str, access_key: str, secret_key: str,
            region: str, now: datetime, service: str = "s3") -> dict[str, str]:
    """Pure: headers to send (incl. Authorization) for one request. `headers` must already hold `host`."""
    parts = urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    signed = {k.lower(): v.strip() for k, v in headers.items()}
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_sha256
    names = sorted(signed)
    query = "&".join(sorted(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, _, v in (p.partition("=") for p in parts.query.split("&") if p)
    ))
    canonical = "\n".join([
        method,
        parts.path or "/",   # already URL-encoded by the caller; S3 signs it as sent (no double encoding)
        query,
        "".join(f"{n}:{signed[n]}\n" for n in names),
        ";".join(names),
        payload_sha256,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **signed,
        "authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={';'.join(names)}, "
                         f"Signature={signature}",
    }


@dataclass(frozen=True)
class ObjectStoreConfig:
    endpoint: str
    region: str
    bucket: str
    prefix: str
    access_key: str
    secret_key: str


class ObjectStore:
    def __init__(self, cfg: ObjectStoreConfig, http: httpx.AsyncClient) -> None:
        self._cfg = cfg
        self._http = http

    @classmethod
    def from_settings(cls, settings, http: httpx.AsyncClient) -> "ObjectStore | None":
        """None (pictures switched off) unless endpoint, bucket and both keys are set."""
        key_id, secret = settings.s3_access_key_id, settings.s3_secret_access_key
        if not (settings.s3_endpoint and settings.s3_bucket and key_id and secret):
            return None
        return cls(ObjectStoreConfig(
            endpoint=settings.s3_endpoint.get_secret_value().rstrip("/"), region=settings.s3_region,
            bucket=settings.s3_bucket, prefix=settings.s3_prefix, access_key=key_id.get_secret_value(),
            secret_key=secret.get_secret_value(),
        ), http)

    def _url(self, key: str) -> str:
        if not key or ".." in key.split("/") or key.startswith("/"):
            raise ValueError(f"bad object key: {key!r}")
        return f"{self._cfg.endpoint}/{self._cfg.bucket}/{quote(self._cfg.prefix + key, safe='/-_.~')}"

    async def _send(self, method: str, key: str, body: bytes = b"", extra: dict[str, str] | None = None) -> httpx.Response:
        url = self._url(key)
        headers = sign_v4(
            method=method, url=url, headers={"host": urlsplit(url).netloc, **(extra or {})},
            payload_sha256=hashlib.sha256(body).hexdigest() if body else _EMPTY_SHA256,
            access_key=self._cfg.access_key, secret_key=self._cfg.secret_key, region=self._cfg.region,
            now=datetime.now(UTC),
        )
        headers.pop("host")  # httpx sets it (same value)
        try:
            return await self._http.request(method, url, content=body or None, headers=headers, timeout=20)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"Object storage unreachable: {type(exc).__name__}") from exc

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        resp = await self._send("PUT", key, data, {"content-type": content_type})
        if resp.status_code >= 300:
            raise UpstreamError(f"Object storage PUT failed: HTTP {resp.status_code}")

    async def get(self, key: str) -> tuple[bytes, str]:
        resp = await self._send("GET", key)
        if resp.status_code == 404:
            raise NotFoundError("Picture not found")
        if resp.status_code >= 300:
            raise UpstreamError(f"Object storage GET failed: HTTP {resp.status_code}")
        return resp.content, resp.headers.get("content-type", "application/octet-stream")

    async def delete(self, key: str) -> None:
        resp = await self._send("DELETE", key)
        if resp.status_code >= 300 and resp.status_code != 404:   # already gone = fine
            raise UpstreamError(f"Object storage DELETE failed: HTTP {resp.status_code}")
