from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

import httpx

from zagent.domain.errors import ValidationError
from zagent.security.secrets import SecretStore


class MCPOAuthManager:
    """OAuth 2.1-style authorization-code + PKCE flow for remote MCP resources."""

    def __init__(
        self,
        data_dir: str,
        secrets_store: SecretStore,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._pending_path = Path(data_dir) / "mcp-oauth-pending.json"
        self._secrets = secrets_store
        self._client = client or httpx.Client(timeout=20, follow_redirects=False)
        self._owns_client = client is None
        self._lock = threading.RLock()

    def begin(
        self,
        server_name: str,
        resource_url: str,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
    ) -> Dict[str, Any]:
        self._validate_redirect(redirect_uri)
        self._validate_endpoint(resource_url, allow_localhost=True)
        resource_metadata = self._resource_metadata(resource_url)
        authorization_servers = resource_metadata.get("authorization_servers", [])
        if not isinstance(authorization_servers, list) or not authorization_servers:
            raise ValidationError("protected resource metadata has no authorization server")
        issuer = str(authorization_servers[0]).rstrip("/")
        self._validate_endpoint(issuer)
        metadata = self._authorization_metadata(issuer)
        if not client_id.strip():
            client_id = self._dynamic_client_registration(metadata, redirect_uri)
        authorization_endpoint = str(metadata.get("authorization_endpoint", ""))
        token_endpoint = str(metadata.get("token_endpoint", ""))
        self._validate_endpoint(authorization_endpoint)
        self._validate_endpoint(token_endpoint)
        methods = metadata.get("code_challenge_methods_supported", [])
        if not authorization_endpoint or not token_endpoint or "S256" not in methods:
            raise ValidationError("authorization server must provide endpoints and PKCE S256")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        resource = self._resource_origin(resource_url)
        effective_scopes = scopes or [str(item) for item in resource_metadata.get("scopes_supported", [])]
        pending = {
            "server_name": server_name,
            "state": state,
            "verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": resource,
            "token_endpoint": token_endpoint,
            "scope": " ".join(effective_scopes),
            "created_at": int(time.time()),
        }
        with self._lock:
            values = self._read_pending()
            values[state] = pending
            self._write_pending(values)
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": resource,
        }
        if effective_scopes:
            query["scope"] = " ".join(effective_scopes)
        return {
            "authorization_url": f"{authorization_endpoint}?{urlencode(query)}",
            "state": state,
            "expires_in": 600,
        }

    def complete(self, state: str, code: str) -> Dict[str, Any]:
        if not state or not code:
            raise ValidationError("OAuth callback requires state and code")
        with self._lock:
            values = self._read_pending()
            pending = values.get(state)
        if not isinstance(pending, dict) or not secrets.compare_digest(str(pending.get("state", "")), state):
            raise ValidationError("invalid OAuth state")
        if int(time.time()) - int(pending.get("created_at", 0)) > 600:
            raise ValidationError("OAuth authorization request expired")
        response = self._client.post(
            str(pending["token_endpoint"]),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": pending["redirect_uri"],
                "client_id": pending["client_id"],
                "code_verifier": pending["verifier"],
                "resource": pending["resource"],
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            raise ValidationError(f"OAuth token exchange failed with status {response.status_code}")
        try:
            token = response.json()
        except ValueError as exc:
            raise ValidationError("OAuth token endpoint returned invalid JSON") from exc
        if not isinstance(token, dict) or not isinstance(token.get("access_token"), str):
            raise ValidationError("OAuth token endpoint returned an invalid response")
        token["obtained_at"] = int(time.time())
        token["token_endpoint"] = pending["token_endpoint"]
        token["client_id"] = pending["client_id"]
        token["resource"] = pending["resource"]
        self._secrets.set(self._secret_name(str(pending["server_name"])), json.dumps(token))
        with self._lock:
            values = self._read_pending()
            values.pop(state, None)
            self._write_pending(values)
        return {"server_name": pending["server_name"], "authorized": True}

    def access_token(self, server_name: str) -> str:
        raw = self._secrets.get(self._secret_name(server_name))
        if not raw:
            return ""
        try:
            token = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        if not isinstance(token, dict):
            return ""
        expires_in = int(token.get("expires_in", 0) or 0)
        obtained_at = int(token.get("obtained_at", 0) or 0)
        if expires_in and time.time() >= obtained_at + expires_in - 30:
            return self._refresh(server_name, token)
        return str(token.get("access_token", ""))

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _refresh(self, server_name: str, token: Dict[str, Any]) -> str:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            return ""
        response = self._client.post(
            str(token.get("token_endpoint", "")),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": token.get("client_id", ""),
                "resource": token.get("resource", ""),
            },
            headers={"Accept": "application/json"},
        )
        if response.status_code >= 400:
            return ""
        try:
            updated = response.json()
        except ValueError:
            return ""
        if not isinstance(updated, dict) or not updated.get("access_token"):
            return ""
        if "refresh_token" not in updated:
            updated["refresh_token"] = refresh_token
        for key in ("token_endpoint", "client_id", "resource"):
            updated[key] = token.get(key)
        updated["obtained_at"] = int(time.time())
        self._secrets.set(self._secret_name(server_name), json.dumps(updated))
        return str(updated["access_token"])

    def _resource_metadata(self, resource_url: str) -> Dict[str, Any]:
        parsed = urlparse(resource_url)
        path = parsed.path.rstrip("/")
        metadata_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{path}"
        candidates = [metadata_url]
        root_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource"
        if root_url != metadata_url:
            candidates.append(root_url)
        return self._first_metadata(candidates, "protected resource")

    def _authorization_metadata(self, issuer: str) -> Dict[str, Any]:
        parsed = urlparse(issuer)
        path = parsed.path.rstrip("/")
        url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{path}"
        oidc_path = f"{parsed.scheme}://{parsed.netloc}/.well-known/openid-configuration{path}"
        oidc_issuer = f"{issuer}/.well-known/openid-configuration"
        metadata = self._first_metadata([url, oidc_path, oidc_issuer], "authorization server")
        if str(metadata.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise ValidationError("authorization server issuer metadata mismatch")
        return metadata

    def _dynamic_client_registration(self, metadata: Dict[str, Any], redirect_uri: str) -> str:
        endpoint = metadata.get("registration_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValidationError("OAuth client_id is required; server has no registration endpoint")
        self._validate_endpoint(endpoint)
        response = self._client.post(
            endpoint,
            json={
                "client_name": "Z-Agent Desktop",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
            headers={"Accept": "application/json"},
        )
        try:
            value = response.json() if response.status_code < 400 else {}
        except ValueError:
            value = {}
        client_id = value.get("client_id") if isinstance(value, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise ValidationError("OAuth dynamic client registration failed")
        return client_id

    def _first_metadata(self, urls: list[str], label: str) -> Dict[str, Any]:
        for url in dict.fromkeys(urls):
            response = self._client.get(url, headers={"Accept": "application/json"})
            if response.status_code == 200:
                try:
                    value = response.json()
                except ValueError:
                    continue
                if isinstance(value, dict):
                    return value
        raise ValidationError(f"unable to discover {label} metadata")

    def _read_pending(self) -> Dict[str, Any]:
        if not self._pending_path.is_file():
            return {}
        try:
            value = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_pending(self, value: Dict[str, Any]) -> None:
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._pending_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self._pending_path)

    @staticmethod
    def _resource_origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @staticmethod
    def _validate_redirect(uri: str) -> None:
        parsed = urlparse(uri)
        if parsed.scheme == "https" and parsed.netloc:
            return
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return
        raise ValidationError("OAuth redirect_uri must use HTTPS or a localhost HTTP callback")

    @staticmethod
    def _validate_endpoint(uri: str, *, allow_localhost: bool = False) -> None:
        parsed = urlparse(uri)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValidationError("OAuth endpoint must be an absolute URL without userinfo")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (
            allow_localhost and parsed.scheme == "http" and local
        ):
            raise ValidationError("OAuth endpoints must use HTTPS")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return
        if not address.is_loopback and (
            address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValidationError("OAuth endpoint may not target a private or reserved IP")

    @staticmethod
    def _secret_name(server_name: str) -> str:
        digest = hashlib.sha256(server_name.encode("utf-8")).hexdigest().upper()
        return f"ZAGENT_MCP_OAUTH_{digest}"
