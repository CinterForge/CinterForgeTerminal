#!/usr/bin/env python3
"""
Shared Cloudflare R2 folder sync transport, migrated from the proven Companion uploader.

Config defaults to ~/dev/config.json and provider cloudflare_r2_usdz.
Bucket, endpoint, and remote object prefix all come from the selected JSON provider.
Provider shape matches the existing CinterForge config style:
{
  "cloudflare_r2_usdz": {
    "enabled": true,
    "bucket": "...",
    "endpoint_url": "https://<account>.r2.cloudflarestorage.com",
    "object_prefix": "usdz",
    "region": "auto",
    "access_key_id": "...",
    "secret_access_key": "..."
  }
}

Modes:
  upload-sync:   mirror local folder to remote prefix; uploads changed/new, deletes stale remote objects unless --no-delete is set.
  download-sync: mirror remote prefix to local folder; downloads changed/new, deletes stale local files unless --no-delete is set.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow this script to be executed directly while importing shared build_cinterforge modules.
_BUILD_CINTERFORGE_ROOT = Path(__file__).resolve().parent.parent
if str(_BUILD_CINTERFORGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BUILD_CINTERFORGE_ROOT))

import argparse
import datetime as _dt
import email.utils
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
from pathlib import Path
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import socket
import xml.etree.ElementTree as ET


def _log(msg: str) -> None:
    print(f"[cloudflare_r2_sync] {msg}", flush=True)


_VERBOSE = False


def _verbose_log(msg: str) -> None:
    if _VERBOSE:
        _log(msg)



def _fmt_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0, n))
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{n} B"


def _heartbeat_seconds() -> float:
    raw = os.environ.get("CINTERFORGE_CLOUDFLARE_HEARTBEAT_SECONDS", "10").strip()
    try:
        value = float(raw)
    except Exception:
        value = 10.0
    return max(2.0, value)


def _with_heartbeat(label: str, fn):
    """Run a blocking network operation while emitting periodic progress logs.

    urllib does not expose byte-level progress for the signed PUT/GET request body.
    Large Pack payload cards can therefore make the app look frozen while the
    socket is still active.  Emit a heartbeat until the blocking request returns
    so automation/finalize logs prove the upload or download is still alive.
    """
    done = threading.Event()
    started = time.monotonic()
    interval = _heartbeat_seconds()

    def beat() -> None:
        tick = 1
        while not done.wait(interval):
            elapsed = time.monotonic() - started
            _log(f"heartbeat {label} elapsed={elapsed:.1f}s tick={tick}")
            tick += 1

    thread = threading.Thread(target=beat, name="cloudflare-r2-sync-heartbeat", daemon=True)
    thread.start()
    try:
        return fn()
    finally:
        done.set()


def _trim_slashes(s: str) -> str:
    return (s or "").strip("/")



def _md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_HASH_CACHE_NAME = "cinterforge_sync_hashes.json"

def _load_hash_cache(folder: Path) -> dict[str, dict]:
    path = folder / _HASH_CACHE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def _save_hash_cache(folder: Path, cache: dict[str, dict]) -> None:
    path = folder / _HASH_CACHE_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)

def _cached_md5(path: Path, rel: str, cache: dict[str, dict]) -> str:
    st = path.stat()
    entry = cache.get(rel) if isinstance(cache.get(rel), dict) else None
    if entry and int(entry.get("size", -1)) == st.st_size and int(entry.get("mtime_ns", -1)) == st.st_mtime_ns:
        value = str(entry.get("md5", "")).lower()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            _verbose_log(f"hash-cache hit {rel} size={_fmt_bytes(st.st_size)}")
            return value
    value = _md5_file(path).lower()
    cache[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "md5": value}
    return value


def _candidate_config_paths(requested: Path) -> list[Path]:
    """Find R2 configuration outside managed VM HOME, without copying secrets.

    Colima/Lima's hermetic Setup replaces HOME with Builds/CFTVM/home. The
    Cloudflare credential file belongs to the *login account*, so use the
    OS account database before defaulting to the temporary managed home.
    """
    candidates: list[Path] = []
    explicit = (os.environ.get('CINTERFORGE_CLOUDFLARE_CONFIG') or
                os.environ.get('CINTER_CLOUDFLARE_CONFIG'))
    homes = []
    if os.name != 'nt':
        try:
            import pwd
            homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
        except (ImportError, KeyError, OSError):
            pass
    homes.append(Path.home())
    for p in [Path(explicit).expanduser()] if explicit else []:
        if p not in candidates:
            candidates.append(p)
    request = requested.expanduser()
    if request not in candidates:
        candidates.append(request)
    for home in homes:
        for directory in ('cfdev', 'dev'):
            path = home / directory / 'config.json'
            if path not in candidates:
                candidates.append(path)
    return candidates


def _provider_from_config(data: dict, provider: str) -> dict | None:
    # Support both shapes used in CinterForge tooling:
    #   { "cloudflare_r2_usdz": { ... } }
    #   { "providers": { "cloudflare_r2_usdz": { ... } } }
    cfg = data.get(provider)
    if not isinstance(cfg, dict):
        providers = data.get("providers")
        if isinstance(providers, dict):
            cfg = providers.get(provider)
    if not isinstance(cfg, dict):
        return None

    # Match CinterForge KeyMaster provider parsing: runtime values live under
    # provider["env"].  Flatten env-style keys over the provider object so callers
    # can use the same selected provider with either snake_case fields or the
    # CINTERFORGE_* env keys stored in ~/cfdev/config.json.
    merged = dict(cfg)
    env = cfg.get("env")
    if isinstance(env, dict):
        merged.update(env)
    # Keep the older accidental name as a harmless fallback only.
    legacy_env = cfg.get("environment")
    if isinstance(legacy_env, dict):
        merged.update(legacy_env)
    return merged


def _extract_canonical_fields(cfg: dict) -> dict[str, str]:
    aliases = {
        "endpoint_url": [
            "endpoint_url", "endpointUrl", "endpoint",
            "CINTERFORGE_CLOUDFLARE_R2_JSON_ENDPOINT_URL",
            "CINTERFORGE_CLOUDFLARE_R2_DEV_ENDPOINT_URL",
            "CINTERFORGE_CLOUDFLARE_R2_ENDPOINT_URL",
        ],
        "object_prefix": [
            "object_prefix", "objectPrefix", "prefix",
            "CINTERFORGE_CLOUDFLARE_R2_JSON_OBJECT_PREFIX",
            "CINTERFORGE_CLOUDFLARE_R2_DEV_OBJECT_PREFIX",
            "CINTERFORGE_CLOUDFLARE_R2_OBJECT_PREFIX",
        ],
        "access_key_id": [
            "access_key_id", "accessKeyId", "access_key", "accessKey",
            "CINTERFORGE_CLOUDFLARE_R2_JSON_ACCESS_KEY_ID",
            "CINTERFORGE_CLOUDFLARE_R2_DEV_ACCESS_KEY_ID",
            "CINTERFORGE_CLOUDFLARE_R2_ACCESS_KEY_ID",
            "CLOUDFLARE_R2_ACCESS_KEY_ID",
        ],
        "secret_access_key": [
            "secret_access_key", "secretAccessKey", "secret_key", "secretKey",
            "CINTERFORGE_CLOUDFLARE_R2_JSON_SECRET_ACCESS_KEY",
            "CINTERFORGE_CLOUDFLARE_R2_DEV_SECRET_ACCESS_KEY",
            "CINTERFORGE_CLOUDFLARE_R2_SECRET_ACCESS_KEY",
            "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
        ],
        "bucket": [
            "bucket",
            "CINTERFORGE_CLOUDFLARE_R2_JSON_BUCKET",
            "CINTERFORGE_CLOUDFLARE_R2_DEV_BUCKET",
            "CINTERFORGE_CLOUDFLARE_R2_BUCKET",
        ],
        "region": ["region", "CINTERFORGE_CLOUDFLARE_R2_JSON_REGION", "CINTERFORGE_CLOUDFLARE_R2_DEV_REGION", "CINTERFORGE_CLOUDFLARE_R2_REGION"],
    }
    out: dict[str, str] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name in cfg and cfg[name] not in (None, ""):
                out[canonical] = str(cfg[name])
                break
    out.setdefault("region", "auto")
    return out


def _read_config_with_provider(path: Path, provider: str) -> tuple[Path, dict, dict]:
    first_existing: Path | None = None
    searched: list[str] = []
    invalid_provider_hits: list[str] = []

    # Important: do not stop at the first config file that mentions the provider.
    # Pete has older ~/cfdev/config.json files that may contain a placeholder
    # cloudflare_r2_usdz provider without credentials, while ~/dev/config.json has
    # the real env-style provider values.  Keep searching until a provider has the
    # required resolved fields.
    for candidate in _candidate_config_paths(path):
        searched.append(str(candidate))
        if not candidate.exists():
            continue
        if first_existing is None:
            first_existing = candidate
        data = json.loads(candidate.read_text(encoding="utf-8"))
        cfg = _provider_from_config(data, provider)
        if not isinstance(cfg, dict):
            continue

        enabled_value = cfg.get("enabled", cfg.get("CINTERFORGE_CLOUDFLARE_R2_DEV_ENABLED",
                               cfg.get("CINTERFORGE_CLOUDFLARE_R2_JSON_ENABLED", True)))
        if enabled_value is False or str(enabled_value).strip().lower() in ("0", "false", "no", "off"):
            invalid_provider_hits.append(f"{candidate}: provider disabled")
            continue

        resolved = _extract_canonical_fields(cfg)
        missing = [k for k in ["bucket", "endpoint_url", "access_key_id", "secret_access_key"] if not resolved.get(k)]
        if missing:
            invalid_provider_hits.append(f"{candidate}: missing {', '.join(missing)}")
            continue

        if candidate != path.expanduser():
            _log(f"using alternate config with provider '{provider}': {candidate}")
        if invalid_provider_hits:
            _log("skipped incomplete provider config(s): " + "; ".join(invalid_provider_hits))
        return candidate, cfg, resolved

    if first_existing is None:
        raise FileNotFoundError(f"No config file found. Searched: {', '.join(searched)}")
    if invalid_provider_hits:
        raise KeyError(
            f"Provider '{provider}' was found but no complete config was usable. "
            f"Details: {'; '.join(invalid_provider_hits)}. Searched: {', '.join(searched)}"
        )
    raise KeyError(f"Provider '{provider}' not found. Searched: {', '.join(searched)}")


def _provider_names(data: dict) -> list[str]:
    """Discover configured R2 providers without reading or logging credentials."""
    names = set()
    for source in (data, data.get("providers")):
        if isinstance(source, dict):
            names.update(name for name, value in source.items()
                         if isinstance(name, str) and isinstance(value, dict)
                         and (name.startswith("cloudflare_r2") or name == "cdn-assets-dev"))
    return sorted(names)


def _read_config_for_bucket(path: Path, required_bucket: str) -> tuple[Path, str, dict]:
    """Find an existing usable R2 provider for a specific, allowed bucket.

    The Companion product does not require a second copy of credentials under
    the name cloudflare_r2_companion. Reuse a configured private-bucket provider
    while refusing to guess a target when multiple unrelated accounts match.
    """
    candidates = _candidate_config_paths(path)
    matches: list[tuple[Path, str, dict]] = []
    found_names: set[str] = set()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        data = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"R2 configuration must be an object: {candidate}")
        for name in _provider_names(data):
            found_names.add(name)
            cfg = _provider_from_config(data, name)
            if cfg is None:
                continue
            enabled = cfg.get("enabled", cfg.get("CINTERFORGE_CLOUDFLARE_R2_DEV_ENABLED",
                       cfg.get("CINTERFORGE_CLOUDFLARE_R2_JSON_ENABLED", True)))
            if enabled is False or str(enabled).strip().lower() in ("0", "false", "no", "off"):
                continue
            resolved = _extract_canonical_fields(cfg)
            if (resolved.get("bucket") == required_bucket and
                    all(resolved.get(key) for key in
                        ("bucket", "endpoint_url", "access_key_id", "secret_access_key"))):
                matches.append((candidate, name, resolved))

    if not matches:
        searched = ", ".join(str(p) for p in candidates)
        providers = ", ".join(sorted(found_names)) or "(none)"
        raise KeyError(f"No enabled, complete Cloudflare R2 provider targets bucket '{required_bucket}'. "
                       f"Found providers: {providers}. Searched: {searched}")

    # A bucket name alone does not identify an R2 account. Do not route a
    # default upload across independently configured R2 accounts by guesswork.
    accounts = {entry[2]["endpoint_url"].rstrip("/") for entry in matches}
    if len(accounts) != 1:
        providers = ", ".join(sorted({name for _, name, _ in matches}))
        raise RuntimeError("Multiple Cloudflare R2 accounts target the required bucket "
                           f"'{required_bucket}' ({providers}); select one with --provider")

    # Prefer the product-specific provider, then the established shared USDZ
    # provider. An explicit --provider always bypasses automatic selection.
    for preferred in ("cloudflare_r2_companion", "cloudflare_r2_usdz"):
        preferred_matches = [entry for entry in matches if entry[1] == preferred]
        if preferred_matches:
            selected = preferred_matches[0]
            break
    else:
        selected = matches[0]
    return selected


def _load_config(path: Path, provider: str, *, required_bucket: str = "") -> dict:
    # Backwards compatibility for Companion installs that still send the
    # retired provider alias. This alias is *always* private-scoped, including
    # calls from older lifecycle versions that omit --require-bucket. A missing
    # alias may select another configured provider for that SAME bucket, but an
    # explicitly configured alias pointing at the wrong bucket must fail.
    legacy_companion = provider == "cloudflare_r2_companion"
    if legacy_companion:
        if required_bucket and required_bucket != "cdn-assets-dev":
            raise RuntimeError("Companion R2 sync may only use private bucket cdn-assets-dev")
        required_bucket = "cdn-assets-dev"
    if provider == "auto":
        if not required_bucket:
            raise ValueError("Automatic R2 provider selection requires --require-bucket")
        resolved_path, provider, out = _read_config_for_bucket(path, required_bucket)
    else:
        try:
            resolved_path, _cfg, out = _read_config_with_provider(path, provider)
        except KeyError as exc:
            if not legacy_companion or "not found" not in str(exc):
                raise
            _log("legacy Companion provider alias absent; selecting an existing "
                 "complete provider for private bucket cdn-assets-dev")
            resolved_path, provider, out = _read_config_for_bucket(path, required_bucket)
    if required_bucket and out["bucket"] != required_bucket:
        raise RuntimeError(f"Cloudflare provider '{provider}' targets bucket '{out['bucket']}', "
                           f"but this operation requires private bucket '{required_bucket}'")
    out["object_prefix"] = _trim_slashes(out.get("object_prefix", "assets"))
    out["endpoint_url"] = out["endpoint_url"].rstrip("/")
    _log(
        f"provider={provider} config={resolved_path} bucket={out['bucket']} "
        f"object_prefix={out.get('object_prefix', '') or '(root)'}"
    )
    return out

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _aws_sig_v4_headers(cfg: dict, method: str, canonical_uri: str, query: str, payload_hash: str, extra_headers: dict[str, str]) -> dict[str, str]:
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    host = urllib.parse.urlparse(cfg["endpoint_url"]).netloc

    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    headers.update({k.lower(): v for k, v in extra_headers.items()})
    signed_headers = ";".join(sorted(headers.keys()))
    canonical_headers = "".join(f"{k}:{str(headers[k]).strip()}\n" for k in sorted(headers.keys()))
    canonical_request = "\n".join([method, canonical_uri, query, canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{cfg['region']}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _sign(_sign(_sign(_sign(("AWS4" + cfg["secret_access_key"]).encode("utf-8"), date_stamp), cfg["region"]), "s3"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        "AWS4-HMAC-SHA256 "
        f"Credential={cfg['access_key_id']}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


def _canonical_uri(bucket: str, key: str = "") -> str:
    path = bucket if not key else f"{bucket}/{key}"
    return "/" + urllib.parse.quote(path, safe="/-_.~")


def _request(cfg: dict, method: str, key: str = "", query_params: dict[str, str] | None = None, body: bytes = b"", headers: dict[str, str] | None = None) -> tuple[int, dict, bytes]:
    query_params = query_params or {}
    headers = headers or {}
    transient_statuses = {429, 500, 502, 503, 504}
    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        canonical_uri = _canonical_uri(cfg["bucket"], key)
        query = urllib.parse.urlencode(sorted(query_params.items()), quote_via=urllib.parse.quote)
        payload_hash = hashlib.sha256(body).hexdigest()
        signed_headers = _aws_sig_v4_headers(cfg, method, canonical_uri, query, payload_hash, headers)
        url = cfg["endpoint_url"] + canonical_uri + (("?" + query) if query else "")
        req = urllib.request.Request(url, data=body if method not in ("GET", "HEAD") else None, method=method)
        for k, v in signed_headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            if e.code in transient_statuses and attempt < max_attempts:
                delay = float(2 ** (attempt - 1))
                _log(
                    f"retry {method} {key or '/'} after HTTP {e.code} "
                    f"attempt={attempt}/{max_attempts} delay={delay:.1f}s"
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"R2 {method} {key or '/'} failed: HTTP {e.code}: {data[:1000]!r}") from e


def _list_remote(cfg: dict, prefix: str) -> dict[str, dict]:
    prefix = _trim_slashes(prefix)
    if prefix:
        prefix = prefix + "/"
    out: dict[str, dict] = {}
    continuation: str | None = None
    while True:
        qp = {"list-type": "2", "prefix": prefix}
        if continuation:
            qp["continuation-token"] = continuation
        _status, _headers, body = _request(cfg, "GET", query_params=qp)
        root = ET.fromstring(body)
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}" if root.tag.startswith("{") else ""
        for contents in root.findall(f"{ns}Contents"):
            key_el = contents.find(f"{ns}Key")
            size_el = contents.find(f"{ns}Size")
            etag_el = contents.find(f"{ns}ETag")
            modified_el = contents.find(f"{ns}LastModified")
            if key_el is None or not key_el.text:
                continue
            key = key_el.text
            if key.endswith("/"):
                continue
            rel = key[len(prefix):] if key.startswith(prefix) else key
            out[rel] = {"key": key, "size": int(size_el.text or "0") if size_el is not None else 0, "etag": (etag_el.text or "").strip('"') if etag_el is not None else "",
                        "last_modified": (modified_el.text or "") if modified_el is not None else ""}
        trunc_el = root.find(f"{ns}IsTruncated")
        if trunc_el is None or (trunc_el.text or "").lower() != "true":
            break
        token_el = root.find(f"{ns}NextContinuationToken")
        continuation = token_el.text if token_el is not None else None
        if not continuation:
            break
    return out


def _should_skip_filesystem_entry(path: Path) -> bool:
    """Return True for local metadata/temp files that must never be synced."""
    for part in path.parts:
        if part == _HASH_CACHE_NAME or part.startswith("."):
            return True
    return False


def _local_files(folder: Path, extension: str, *, exclude_relative: tuple[str, ...] = ()) -> dict[str, Path]:
    if not folder.exists():
        return {}
    ext = (extension or "").lower()
    excluded = {str(value).replace("\\", "/").lstrip("/") for value in exclude_relative if str(value).strip()}
    out: dict[str, Path] = {}
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        rel_path = p.relative_to(folder)
        if _should_skip_filesystem_entry(rel_path):
            continue
        if rel_path.as_posix() in excluded:
            continue
        # Empty extension means "all regular files".  This keeps the helper
        # usable for generic syncs without accidentally excluding .json/.zip.
        if ext and p.suffix.lower() != ext:
            continue
        out[rel_path.as_posix()] = p
    return out


_MULTIPART_THRESHOLD_BYTES = 100 * 1024 * 1024
_MULTIPART_PART_BYTES = 32 * 1024 * 1024


def _multipart_create(cfg: dict, key: str, content_type: str) -> str:
    _status, _headers, body = _request(
        cfg,
        "POST",
        key=key,
        query_params={"uploads": ""},
        headers={"content-type": content_type},
    )
    root = ET.fromstring(body)
    upload_id = root.findtext("{*}UploadId") or root.findtext("UploadId")
    if not upload_id:
        raise RuntimeError(f"R2 multipart create did not return UploadId for {key}")
    return upload_id


def _multipart_abort(cfg: dict, key: str, upload_id: str) -> None:
    _request(cfg, "DELETE", key=key, query_params={"uploadId": upload_id})


def _multipart_complete(cfg: dict, key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
    root = ET.Element("CompleteMultipartUpload")
    for part_number, etag in parts:
        part = ET.SubElement(root, "Part")
        ET.SubElement(part, "PartNumber").text = str(part_number)
        ET.SubElement(part, "ETag").text = etag
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    _request(
        cfg,
        "POST",
        key=key,
        query_params={"uploadId": upload_id},
        body=body,
        headers={"content-type": "application/xml"},
    )


def _put_file(
    cfg: dict,
    key: str,
    path: Path,
    *,
    uploaded_before: int = 0,
    upload_total: int = 0,
) -> None:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    size = path.stat().st_size
    if size < _MULTIPART_THRESHOLD_BYTES:
        _log(f"read upload body {path} size={_fmt_bytes(size)}")
        body = path.read_bytes()
        _with_heartbeat(
            f"PUT {key} size={_fmt_bytes(size)}",
            lambda: _request(cfg, "PUT", key=key, body=body, headers={"content-type": content_type}),
        )
        if upload_total:
            _log(f"upload-progress bytes={uploaded_before + size} total_bytes={upload_total} file={path.name}")
        return

    part_size = _MULTIPART_PART_BYTES
    total_parts = (size + part_size - 1) // part_size
    _log(
        f"multipart start {path.name} size={_fmt_bytes(size)} parts={total_parts} "
        f"part_size={_fmt_bytes(part_size)} -> {key}"
    )
    upload_id = _multipart_create(cfg, key, content_type)
    parts: list[tuple[int, str]] = []
    sent = 0
    try:
        with path.open("rb") as handle:
            for part_number in range(1, total_parts + 1):
                body = handle.read(part_size)
                if not body:
                    raise RuntimeError(f"Unexpected EOF while reading multipart upload {path}")
                _status, response_headers, _response_body = _with_heartbeat(
                    f"UPLOAD-PART {key} part={part_number}/{total_parts} size={_fmt_bytes(len(body))}",
                    lambda body=body, part_number=part_number: _request(
                        cfg,
                        "PUT",
                        key=key,
                        query_params={"partNumber": str(part_number), "uploadId": upload_id},
                        body=body,
                    ),
                )
                etag = str(response_headers.get("ETag") or response_headers.get("Etag") or response_headers.get("etag") or "").strip()
                if not etag:
                    raise RuntimeError(f"R2 multipart part {part_number} returned no ETag for {key}")
                parts.append((part_number, etag))
                sent += len(body)
                cumulative = uploaded_before + sent
                _log(
                    f"upload-progress bytes={cumulative} total_bytes={upload_total or size} "
                    f"file={path.name} part={part_number}/{total_parts}"
                )
        _multipart_complete(cfg, key, upload_id, parts)
        _log(f"multipart complete {path.name} parts={total_parts} size={_fmt_bytes(size)}")
    except Exception:
        try:
            _multipart_abort(cfg, key, upload_id)
            _log(f"multipart aborted {path.name} upload_id={upload_id}")
        except Exception as abort_exc:
            _log(f"multipart abort failed {path.name}: {abort_exc}")
        raise


def _download_timeout_seconds() -> float:
    raw = os.environ.get("CINTERFORGE_CLOUDFLARE_IO_TIMEOUT_SECONDS", "120").strip()
    try:
        value = float(raw)
    except Exception:
        value = 120.0
    return max(15.0, value)


def _get_file(cfg: dict, key: str, path: Path, expected_size: int | None = None) -> None:
    """Download one R2 object atomically with resume, progress, timeout, and retry.

    Large search databases must never be accumulated in RAM or allowed to block
    forever inside urllib.  A ``.part`` file is durable across setup reruns and
    each retry resumes with a signed Range request.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    if path.is_file() and expected_size is not None and path.stat().st_size == expected_size:
        return
    if path.exists():
        path.unlink()
    timeout = _download_timeout_seconds()
    max_attempts = 6
    chunk_size = 1024 * 1024
    for attempt in range(1, max_attempts + 1):
        offset = partial.stat().st_size if partial.is_file() else 0
        if expected_size is not None and offset > expected_size:
            partial.unlink(missing_ok=True)
            offset = 0
        request_headers = {"range": f"bytes={offset}-"} if offset else {}
        canonical_uri = _canonical_uri(cfg["bucket"], key)
        payload_hash = hashlib.sha256(b"").hexdigest()
        signed_headers = _aws_sig_v4_headers(cfg, "GET", canonical_uri, "", payload_hash, request_headers)
        url = cfg["endpoint_url"] + canonical_uri
        req = urllib.request.Request(url, method="GET")
        for name, value in signed_headers.items():
            req.add_header(name, value)
        started = time.monotonic()
        last_report = started
        downloaded_this_attempt = 0
        try:
            _log(
                f"download stream {key} attempt={attempt}/{max_attempts} "
                f"resume={_fmt_bytes(offset)} expected={_fmt_bytes(expected_size or 0)} timeout={timeout:.0f}s"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 200))
                # R2 may legally ignore Range and return the full object. Never
                # append a 200 response to an existing partial file.
                if offset and status != 206:
                    _log(f"server did not honor Range for {key}; restarting object from byte 0")
                    partial.unlink(missing_ok=True)
                    offset = 0
                mode = "ab" if offset and status == 206 else "wb"
                with partial.open(mode) as output:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded_this_attempt += len(chunk)
                        now = time.monotonic()
                        if now - last_report >= _heartbeat_seconds():
                            current = offset + downloaded_this_attempt
                            pct = (100.0 * current / expected_size) if expected_size else 0.0
                            suffix = f" {pct:.1f}%" if expected_size else ""
                            _log(f"download progress {key} {_fmt_bytes(current)}/{_fmt_bytes(expected_size or 0)}{suffix}")
                            last_report = now
            final_size = partial.stat().st_size
            if expected_size is not None and final_size != expected_size:
                raise RuntimeError(f"partial object size after GET: got {final_size}, expected {expected_size}")
            os.replace(partial, path)
            _log(f"download verified {key} size={_fmt_bytes(final_size)} elapsed={time.monotonic()-started:.1f}s")
            return
        except KeyboardInterrupt:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, socket.timeout, OSError, RuntimeError) as exc:
            if attempt >= max_attempts:
                raise RuntimeError(
                    f"R2 GET {key} failed after {max_attempts} attempts; "
                    f"partial preserved at {partial} ({_fmt_bytes(partial.stat().st_size if partial.exists() else 0)}): {exc}"
                ) from exc
            delay = min(30.0, float(2 ** (attempt - 1)))
            _log(
                f"download retry {key} attempt={attempt}/{max_attempts} "
                f"partial={_fmt_bytes(partial.stat().st_size if partial.exists() else 0)} delay={delay:.1f}s error={exc}"
            )
            time.sleep(delay)


def _delete(cfg: dict, key: str) -> None:
    _request(cfg, "DELETE", key=key)


def ensure_public_asset_cors(cfg: dict) -> None:
    """Make browser retrieval from the public R2 bucket origin-independent.

    CinterForge stores only relative managed-asset paths.  Editor and packaged
    runtimes resolve those paths to the bucket's public base URL, so the bucket
    itself must permit direct GET/HEAD requests from any origin (including
    localhost authoring and serverless/file-hosted runtimes).
    """
    root = ET.Element("CORSConfiguration", xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
    rule = ET.SubElement(root, "CORSRule")
    ET.SubElement(rule, "ID").text = "cinterforge-public-assets"
    ET.SubElement(rule, "AllowedOrigin").text = "*"
    ET.SubElement(rule, "AllowedMethod").text = "GET"
    ET.SubElement(rule, "AllowedMethod").text = "HEAD"
    ET.SubElement(rule, "AllowedHeader").text = "*"
    for header in ("ETag", "Content-Length", "Content-Type", "Accept-Ranges"):
        ET.SubElement(rule, "ExposeHeader").text = header
    ET.SubElement(rule, "MaxAgeSeconds").text = "86400"
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    _request(
        cfg,
        "PUT",
        query_params={"cors": ""},
        body=body,
        headers={"content-type": "application/xml"},
    )
    # Read it back.  A successful PUT alone is not sufficient: publication must
    # fail loudly if the browser-access policy was not actually committed.
    _status, _headers, verified = _request(cfg, "GET", query_params={"cors": ""})
    parsed = ET.fromstring(verified)
    origins = {str(el.text or "") for el in parsed.findall("{*}CORSRule/{*}AllowedOrigin")}
    methods = {str(el.text or "") for el in parsed.findall("{*}CORSRule/{*}AllowedMethod")}
    if "*" not in origins or not {"GET", "HEAD"}.issubset(methods):
        raise RuntimeError(
            f"R2 browser CORS verification failed for bucket {cfg['bucket']}: "
            f"origins={sorted(origins)!r} methods={sorted(methods)!r}"
        )
    _log(f"browser CORS verified bucket={cfg['bucket']} origins=* methods=GET,HEAD")


def upload_sync(
    cfg: dict,
    folder: Path,
    prefix: str,
    extension: str,
    *,
    no_delete: bool = False,
    preserve_remote_subprefixes: tuple[str, ...] = (),
    exclude_relative: tuple[str, ...] = (),
) -> None:
    prefix = _trim_slashes(prefix or cfg.get("object_prefix") or "assets")
    # Bucket CORS is infrastructure policy, not content publication.  Content
    # publishers may have object-write permission without PutBucketCors.
    local = _local_files(folder, extension, exclude_relative=exclude_relative)
    remote = _list_remote(cfg, prefix)
    total_local_bytes = sum(path.stat().st_size for path in local.values())
    _log(
        f"upload-sync local={folder} remote={cfg['bucket']}/{prefix} "
        f"local_files={len(local)} local_bytes={_fmt_bytes(total_local_bytes)} remote_files={len(remote)}"
    )
    uploaded = 0
    added = 0
    changed = 0
    uploaded_bytes = 0
    unchanged = 0
    hash_cache = _load_hash_cache(folder)
    hash_cache_dirty = False
    local_items = sorted(local.items())
    total_files = len(local_items)
    for index, (rel, path) in enumerate(local_items, start=1):
        key = f"{prefix}/{rel}" if prefix else rel
        size = path.stat().st_size
        r = remote.get(rel)
        # R2/S3 ETag is usually the MD5 for these single-part setup uploads.
        # Do not rely on size alone: regenerated USDZ/zip files can keep the same
        # byte count while their contents changed.  If the remote exposes a normal
        # 32-char MD5 ETag, compare it; otherwise fall back to size.
        if r and int(r.get("size", -1)) == size:
            etag = str(r.get("etag", "")).strip('"').lower()
            if len(etag) == 32 and all(c in "0123456789abcdef" for c in etag):
                before = hash_cache.get(rel)
                local_md5 = _cached_md5(path, rel, hash_cache)
                if hash_cache.get(rel) != before:
                    hash_cache_dirty = True
                if local_md5 == etag:
                    unchanged += 1
                    _verbose_log(f"unchanged {rel} ({index}/{total_files}) size={_fmt_bytes(size)}")
                    continue
            else:
                unchanged += 1
                _verbose_log(f"unchanged {rel} ({index}/{total_files}) size={_fmt_bytes(size)}")
                continue
        _log(f"upload start {rel} ({index}/{total_files}) size={_fmt_bytes(size)} -> {key}")
        started = time.monotonic()
        _put_file(cfg, key, path, uploaded_before=uploaded_bytes, upload_total=total_local_bytes)
        elapsed = time.monotonic() - started
        uploaded += 1
        if r is None:
            added += 1
        else:
            changed += 1
        uploaded_bytes += size
        _log(
            f"upload done {rel} ({index}/{total_files}) elapsed={elapsed:.1f}s "
            f"uploaded_files={uploaded} uploaded_bytes={_fmt_bytes(uploaded_bytes)}"
        )
    if hash_cache_dirty:
        _save_hash_cache(folder, hash_cache)
    ext = (extension or "").lower()
    preserved_prefixes = tuple(
        _trim_slashes(value) + "/"
        for value in preserve_remote_subprefixes
        if _trim_slashes(value)
    )
    deleted = 0
    if no_delete:
        stale = [r["key"] for rel, r in sorted(remote.items()) if (not ext or rel.lower().endswith(ext)) and rel not in local]
        if stale:
            _log(f"no-delete enabled; preserved {len(stale)} remote stale object(s)")
    else:
        for rel, r in sorted(remote.items()):
            if ext and not rel.lower().endswith(ext):
                continue
            if preserved_prefixes and rel.startswith(preserved_prefixes):
                continue
            if rel not in local:
                deleted += 1
                _log(f"delete remote stale {r['key']}")
                _delete(cfg, r["key"])
    # Verify the published surface after all writes/deletes.  A successful PUT is
    # not sufficient evidence for Publish readiness; re-list R2 and require every
    # local object to exist remotely at the expected byte size.
    verified_remote = _list_remote(cfg, prefix)
    verification_failures: list[str] = []
    for rel, path in sorted(local.items()):
        remote_item = verified_remote.get(rel)
        if remote_item is None:
            verification_failures.append(f"missing remote object: {rel}")
            continue
        local_size = path.stat().st_size
        remote_size = int(remote_item.get("size", -1))
        if remote_size != local_size:
            verification_failures.append(
                f"size mismatch: {rel} local={local_size} remote={remote_size}"
            )
    if verification_failures:
        preview = "; ".join(verification_failures[:10])
        if len(verification_failures) > 10:
            preview += f"; ... {len(verification_failures) - 10} more"
        raise RuntimeError(
            f"upload-sync remote verification failed for {cfg['bucket']}/{prefix}: {preview}"
        )
    _log(
        f"upload-sync complete remote={cfg['bucket']}/{prefix} "
        f"uploaded={uploaded} added={added} changed={changed} unchanged={unchanged} deleted={deleted} local_files={len(local)} "
        f"verified_remote_files={len(local)}"
    )


def download_sync(
    cfg: dict,
    folder: Path,
    prefix: str,
    extension: str,
    *,
    no_delete: bool = False,
    bundle_index: str = "",
    backup_replaced: Path | None = None,
) -> None:
    prefix = _trim_slashes(prefix or cfg.get("object_prefix") or "assets")
    folder.mkdir(parents=True, exist_ok=True)
    ext = (extension or "").lower()
    remote_all = {rel: r for rel, r in _list_remote(cfg, prefix).items() if (not ext or rel.lower().endswith(ext))}
    if bundle_index:
        index_rel = _trim_slashes(bundle_index)
        index_remote = remote_all.get(index_rel)
        if index_remote is None:
            raise RuntimeError(f"authoritative bundle index missing from {cfg['bucket']}/{prefix}: {index_rel}")
        index_path = folder / index_rel
        index_path.parent.mkdir(parents=True, exist_ok=True)
        # The index is the commit point for a bundle set. Always refresh it before
        # deciding which immutable content-addressed bundles belong to this revision.
        _get_file(cfg, index_remote["key"], index_path, int(index_remote.get("size", -1)))
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid authoritative bundle index {index_rel}: {exc}") from exc
        bundle_names = {str(row.get("file") or "") for row in (payload.get("bundles") or []) if isinstance(row, dict)}
        bundle_names.discard("")
        wanted = bundle_names | {index_rel}
        missing = sorted(wanted - set(remote_all))
        if missing:
            raise RuntimeError(f"authoritative bundle index references missing remote object(s): {', '.join(missing[:10])}")
        remote = {rel: remote_all[rel] for rel in wanted}
        _log(f"authoritative bundle index {index_rel} selected {len(bundle_names)} bundle(s); ignored {len(remote_all)-len(remote)} stale remote object(s)")
    else:
        remote = remote_all
    local = _local_files(folder, extension)
    total_remote_bytes = sum(int(r.get("size", 0)) for r in remote.values())
    _log(
        f"download-sync remote={cfg['bucket']}/{prefix} local={folder} "
        f"remote_files={len(remote)} remote_bytes={_fmt_bytes(total_remote_bytes)} local_files={len(local)}"
    )
    downloaded = 0
    added = 0
    changed = 0
    unchanged = 0
    remote_items = sorted(remote.items())
    total_files = len(remote_items)
    for index, (rel, r) in enumerate(remote_items, start=1):
        path = folder / rel
        size = int(r.get("size", -1))
        existed = path.is_file()
        same = False
        if existed and path.stat().st_size == size:
            etag = str(r.get("etag", "")).strip('"').lower()
            if len(etag) == 32 and all(c in "0123456789abcdef" for c in etag):
                local_md5 = hashlib.md5(path.read_bytes()).hexdigest()
                same = local_md5 == etag
            else:
                same = True
        if same:
            unchanged += 1
            _verbose_log(f"unchanged {rel} ({index}/{total_files}) size={_fmt_bytes(size)}")
            continue
        if existed and backup_replaced is not None:
            backup_path = backup_replaced / rel
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            # Preserve the exact pre-Pull bytes before replacing the live asset.
            shutil.copy2(path, backup_path)
            _log(f"pending backup {rel} -> {backup_path}")
        _log(f"download start {r['key']} ({index}/{total_files}) size={_fmt_bytes(size)} -> {path}")
        started = time.monotonic()
        _get_file(cfg, r["key"], path, size)
        elapsed = time.monotonic() - started
        downloaded += 1
        if existed:
            changed += 1
        else:
            added += 1
        _log(f"download done {rel} ({index}/{total_files}) elapsed={elapsed:.1f}s downloaded_files={downloaded}")
    deleted = 0
    if no_delete:
        stale = [str(path) for rel, path in sorted(local.items()) if rel not in remote]
        if stale:
            _log(f"no-delete enabled; preserved {len(stale)} local stale file(s)")
    else:
        for rel, path in sorted(local.items()):
            if rel not in remote:
                deleted += 1
                _log(f"delete local stale {path}")
                path.unlink()
    _log(
        f"download-sync complete remote={cfg['bucket']}/{prefix} "
        f"downloaded={downloaded} added={added} changed={changed} unchanged={unchanged} deleted={deleted} remote_files={len(remote)}"
    )


def main(argv: list[str]) -> int:
    default_config = Path.home() / "dev" / "config.json"
    fallback_config = Path.home() / "cfdev" / "config.json"
    if not default_config.exists() and fallback_config.exists():
        default_config = fallback_config
    ap = argparse.ArgumentParser(description="Sync a local folder with a Cloudflare R2 prefix.")
    ap.add_argument("mode", choices=["upload-sync", "download-sync"])
    ap.add_argument("--config", default=str(default_config))
    ap.add_argument("--provider", default="cloudflare_r2_usdz")
    ap.add_argument("--require-bucket", default="", help="Refuse any provider not targeting this exact R2 bucket. Required with --provider auto.")
    ap.add_argument("--local", required=True)
    ap.add_argument("--prefix", default="", help="Optional override. If omitted, uses provider object_prefix from config JSON.")
    ap.add_argument("--extension", default=".usdz")
    ap.add_argument("--no-delete", action="store_true", help="Upload/download changed files without deleting stale files on the destination side.")
    ap.add_argument("--backup-replaced", default="", help="For download-sync, copy each replaced local file here before overwriting it.")
    ap.add_argument("--bundle-index", default="", help="For download-sync, fetch this authoritative JSON index first and download only bundle files referenced by its bundles[].file entries.")
    ap.add_argument(
        "--preserve-remote-subprefix",
        action="append",
        default=[],
        help="For upload-sync deletion only, preserve objects beneath this child prefix relative to --prefix. May be repeated.",
    )
    ap.add_argument("--exclude-relative", action="append", default=[], help="For upload-sync, exclude this exact local path relative to --local. May be repeated.")
    ap.add_argument("--allow-upload", action="store_true", help="Explicitly permit upload-sync. Setup/tester runs must not pass this.")
    ap.add_argument("--verbose", action="store_true", help="Show per-file unchanged and hash-cache-hit diagnostics.")
    args = ap.parse_args(argv)
    global _VERBOSE
    _VERBOSE = bool(args.verbose)
    cfg = _load_config(Path(args.config).expanduser(), args.provider,
                       required_bucket=args.require_bucket)
    local = Path(args.local).expanduser().resolve()
    if args.mode == "upload-sync":
        if os.environ.get("CINTERFORGE_SETUP_DISABLE_UPLOADS") == "1":
            raise RuntimeError("Refusing upload-sync because CINTERFORGE_SETUP_DISABLE_UPLOADS=1; setup/tester machines are download-only")
        if not args.allow_upload and os.environ.get("CINTERFORGE_CLOUDFLARE_UPLOADS_ALLOWED") != "1":
            raise RuntimeError("Refusing upload-sync because CINTERFORGE_CLOUDFLARE_UPLOADS_ALLOWED is not set; setup/tester machines are download-only")
        if not local.exists():
            raise FileNotFoundError(f"Local folder not found for upload-sync: {local}")
        upload_sync(
            cfg,
            local,
            args.prefix,
            args.extension,
            no_delete=args.no_delete,
            preserve_remote_subprefixes=tuple(args.preserve_remote_subprefix),
            exclude_relative=tuple(args.exclude_relative),
        )
    else:
        download_sync(
            cfg, local, args.prefix, args.extension,
            no_delete=args.no_delete,
            bundle_index=args.bundle_index,
            backup_replaced=Path(args.backup_replaced).expanduser().resolve() if args.backup_replaced else None,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        _log(f"ERROR: {exc}")
        raise SystemExit(1)
