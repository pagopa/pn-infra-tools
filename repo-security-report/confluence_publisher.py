#!/usr/bin/env python3
"""Publish a GitHub security report page to Confluence Cloud and attach a CSV."""

from __future__ import annotations

import json
import mimetypes
import os
import uuid
from dataclasses import dataclass
from typing import Callable
from urllib import error, parse, request
from xml.sax.saxutils import escape as xml_escape

DEFAULT_CONFLUENCE_CLOUD_ID = "f985e071-a2d6-442d-a5f4-a425cf8beb72"
DEFAULT_CONFLUENCE_SITE_URL = "https://pagopa.atlassian.net"
DEFAULT_CONFLUENCE_URL = (
    f"https://api.atlassian.com/ex/confluence/{DEFAULT_CONFLUENCE_CLOUD_ID}"
)
DEFAULT_SPACE_KEY = "PN"


def resolve_api_url(url: str | None, cloud_id: str | None) -> str:
    """Use the Atlassian API gateway; scoped tokens 401 on the site URL."""
    resolved_cloud_id = (cloud_id or "").strip() or DEFAULT_CONFLUENCE_CLOUD_ID
    gateway = f"https://api.atlassian.com/ex/confluence/{resolved_cloud_id}"
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return gateway

    parsed = parse.urlparse(raw)
    if (
        parsed.scheme == "https"
        and parsed.hostname == "api.atlassian.com"
        and parsed.path.startswith("/ex/confluence/")
    ):
        return raw
    return gateway


@dataclass
class ConfluenceConfig:
    url: str
    space_key: str
    token: str
    user: str | None = None
    parent_id: str | None = None
    site_url: str = DEFAULT_CONFLUENCE_SITE_URL

    @classmethod
    def from_env(
        cls,
        url: str | None = None,
        space_key: str | None = None,
        token: str | None = None,
        user: str | None = None,
        parent_id: str | None = None,
        cloud_id: str | None = None,
        site_url: str | None = None,
    ) -> "ConfluenceConfig":
        resolved_token = (
            token
            or os.getenv("CONFLUENCE_API_TOKEN")
            or os.getenv("CONFLUENCE_API_KEY")
            or os.getenv("CONFLUENCE_TOKEN")
            or ""
        ).strip()
        resolved_user = (
            user
            or os.getenv("CONFLUENCE_USER")
            or os.getenv("CONFLUENCE_EMAIL")
            or ""
        ).strip() or None
        resolved_cloud_id = (
            cloud_id or os.getenv("CONFLUENCE_CLOUD_ID") or DEFAULT_CONFLUENCE_CLOUD_ID
        ).strip()
        resolved_url = resolve_api_url(
            url or os.getenv("CONFLUENCE_URL"),
            resolved_cloud_id,
        )
        resolved_site = (
            site_url or os.getenv("CONFLUENCE_SITE_URL") or DEFAULT_CONFLUENCE_SITE_URL
        ).strip().rstrip("/")
        resolved_space = (
            space_key or os.getenv("CONFLUENCE_SPACE") or DEFAULT_SPACE_KEY
        ).strip()
        resolved_parent = (
            parent_id or os.getenv("CONFLUENCE_PARENT_ID") or ""
        ).strip() or None
        if not resolved_token:
            raise ValueError(
                "Missing Confluence API token. Set CONFLUENCE_API_TOKEN "
                "(or CONFLUENCE_API_KEY) in .env, or pass --confluence-token."
            )
        return cls(
            url=resolved_url,
            space_key=resolved_space,
            token=resolved_token,
            user=resolved_user,
            parent_id=resolved_parent,
            site_url=resolved_site,
        )


def _wiki_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/wiki") else f"{base}/wiki"


def _authorization_header(config: ConfluenceConfig) -> str:
    if config.user:
        import base64

        raw = f"{config.user}:{config.token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")
    return f"Bearer {config.token}"


class ConfluenceClient:
    def __init__(self, config: ConfluenceConfig) -> None:
        self.config = config
        self.wiki_root = _wiki_root(config.url)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> tuple[object, dict[str, str]]:
        url = f"{self.wiki_root}{path}"
        if query:
            url = f"{url}?{parse.urlencode(query)}"
        headers = {
            "Authorization": _authorization_header(self.config),
            "Accept": "application/json",
            "User-Agent": "pn-github-security-report",
        }
        if extra_headers:
            headers.update(extra_headers)
        data: bytes | None = raw_body
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=60) as resp:
                charset = resp.headers.get_content_charset("utf-8")
                body = resp.read().decode(charset)
                parsed: object = json.loads(body) if body else {}
                return parsed, {k: v for k, v in resp.headers.items()}
        except error.HTTPError as exc:
            details = ""
            try:
                charset = exc.headers.get_content_charset("utf-8") if exc.headers else "utf-8"
                raw = exc.read().decode(charset)
                parsed_err = json.loads(raw)
                if isinstance(parsed_err, dict):
                    details = str(
                        parsed_err.get("message")
                        or parsed_err.get("error")
                        or parsed_err
                    )
                else:
                    details = raw[:500]
            except Exception:
                pass
            message = f"HTTP {exc.code}" + (f": {details}" if details else "")
            raise RuntimeError(f"Confluence API {method} {path} failed ({message})") from exc
        except error.URLError as exc:
            raise RuntimeError(f"Confluence connection error: {exc.reason}") from exc

    def find_page_by_title(self, title: str) -> dict | None:
        payload, _ = self._request(
            "GET",
            "/rest/api/content",
            query={
                "spaceKey": self.config.space_key,
                "title": title,
                "expand": "version",
            },
        )
        if not isinstance(payload, dict):
            return None
        results = payload.get("results") or []
        if not results:
            return None
        page = results[0]
        return page if isinstance(page, dict) else None

    def create_page(self, title: str, storage_html: str) -> dict:
        body: dict = {
            "type": "page",
            "title": title,
            "space": {"key": self.config.space_key},
            "body": {
                "storage": {
                    "value": storage_html,
                    "representation": "storage",
                }
            },
        }
        if self.config.parent_id:
            body["ancestors"] = [{"id": self.config.parent_id}]
        payload, _ = self._request("POST", "/rest/api/content", payload=body)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError("Confluence did not return a page id on create")
        return payload

    def update_page(self, page: dict, title: str, storage_html: str) -> dict:
        page_id = str(page.get("id") or "")
        version = ((page.get("version") or {}).get("number")) or 0
        body: dict = {
            "id": page_id,
            "type": "page",
            "title": title,
            "space": {"key": self.config.space_key},
            "version": {
                "number": int(version) + 1,
                "message": "Aggiornato report di sicurezza GitHub",
            },
            "body": {
                "storage": {
                    "value": storage_html,
                    "representation": "storage",
                }
            },
        }
        if self.config.parent_id:
            body["ancestors"] = [{"id": self.config.parent_id}]
        payload, _ = self._request("PUT", f"/rest/api/content/{page_id}", payload=body)
        if not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError("Confluence did not return a page id on update")
        return payload

    def upsert_page(self, title: str, storage_html: str) -> dict:
        existing = self.find_page_by_title(title)
        if existing:
            return self.update_page(existing, title, storage_html)
        return self.create_page(title, storage_html)

    def _list_attachments(self, page_id: str, filename: str | None = None) -> list[dict]:
        query = {"limit": "100"}
        if filename:
            query["filename"] = filename
        payload, _ = self._request(
            "GET",
            f"/rest/api/content/{page_id}/child/attachment",
            query=query,
        )
        if not isinstance(payload, dict):
            return []
        results = payload.get("results") or []
        return [item for item in results if isinstance(item, dict)]

    def attach_or_replace_file(self, page_id: str, file_path: str, filename: str) -> None:
        with open(file_path, "rb") as handle:
            file_bytes = handle.read()
        content_type = mimetypes.guess_type(filename)[0] or "text/csv"
        attachments = self._list_attachments(page_id, filename)
        existing = next(
            (item for item in attachments if item.get("title") == filename),
            None,
        )
        fields = {"minorEdit": "true", "comment": "CSV dei problemi di sicurezza GitHub"}
        files = {"file": (filename, file_bytes, content_type)}
        body, content_type_header = _encode_multipart(fields, files)
        extra = {"X-Atlassian-Token": "nocheck"}
        if existing and existing.get("id"):
            attachment_id = str(existing["id"])
            self._request(
                "POST",
                f"/rest/api/content/{page_id}/child/attachment/{attachment_id}/data",
                raw_body=body,
                content_type=content_type_header,
                extra_headers=extra,
            )
            return
        self._request(
            "POST",
            f"/rest/api/content/{page_id}/child/attachment",
            raw_body=body,
            content_type=content_type_header,
            extra_headers=extra,
        )

    def page_url(self, page: dict) -> str:
        links = page.get("_links") or {}
        webui = links.get("webui") or ""
        site_wiki = _wiki_root(self.config.site_url)
        if webui:
            return f"{site_wiki.rstrip('/')}{webui}"
        return f"{site_wiki}/pages/{page.get('id')}"


def _encode_multipart(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    boundary = f"----ConfluenceBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        chunks.append(f"{value}\r\n".encode("utf-8"))
    for name, (filename, content, content_type) in files.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _status_lozenge(colour: str, title: str) -> str:
    return (
        f'<ac:structured-macro ac:name="status">'
        f'<ac:parameter ac:name="colour">{xml_escape(colour)}</ac:parameter>'
        f'<ac:parameter ac:name="title">{xml_escape(title)}</ac:parameter>'
        f"</ac:structured-macro>"
    )


def _count_cell(value: int, colour: str | None = None) -> str:
    if value and colour:
        return _status_lozenge(colour, str(value))
    return xml_escape(str(value))


def _count_with_link(value: int, colour: str | None, url: str | None) -> str:
    cell = _count_cell(value, colour)
    if value and url:
        return f"{cell} {_href(url, 'apri')}"
    return cell


def _th(*headers: str) -> str:
    cells = "".join(f"<th>{xml_escape(h)}</th>" for h in headers)
    return f"<tr>{cells}</tr>"


def _td_html(cells: list[str]) -> str:
    return "<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"


def _table(header_row: str, body_rows: list[str]) -> str:
    return (
        '<table class="wrapped"><tbody>'
        + header_row
        + "".join(body_rows)
        + "</tbody></table>"
    )


def _href(url: str, text: str) -> str:
    if not url:
        return xml_escape(text)
    return f'<a href="{xml_escape(url, {chr(34): "&quot;"})}">{xml_escape(text)}</a>'


def _repo_link(repository: str) -> str:
    return _href(f"https://github.com/{repository}", repository)


def _github_path_url(repository: str, path: str) -> str:
    return f"https://github.com/{repository}/{path.lstrip('/')}"


def _csv_download_block(csv_filename: str, csv_is_detailed: bool = True) -> str:
    filename = xml_escape(csv_filename, {chr(34): "&quot;"})
    description = (
        "una riga per ogni vulnerabilità e per ogni PR Renovate."
        if csv_is_detailed else "riepilogo dei conteggi per repository."
    )
    return f"""
<ac:structured-macro ac:name="panel">
  <ac:parameter ac:name="title">Scarica il dettaglio delle segnalazioni</ac:parameter>
  <ac:parameter ac:name="borderColor">#0052CC</ac:parameter>
  <ac:parameter ac:name="bgColor">#DEEBFF</ac:parameter>
  <ac:rich-text-body>
    <p>
      <ac:link>
        <ri:attachment ri:filename="{filename}" />
        <ac:plain-text-link-body><![CDATA[Scarica CSV]]></ac:plain-text-link-body>
      </ac:link>
      — {description}
    </p>
    <ac:structured-macro ac:name="attachments">
      <ac:parameter ac:name="patterns">{filename}</ac:parameter>
      <ac:parameter ac:name="old">false</ac:parameter>
      <ac:parameter ac:name="upload">false</ac:parameter>
    </ac:structured-macro>
  </ac:rich-text-body>
</ac:structured-macro>
""".strip()


_ALERT_STATE_IT = {
    "open": "aperti",
    "dismissed": "ignorati",
    "fixed": "risolti",
    "auto_dismissed": "ignorati automaticamente",
}


def build_report_storage(
    *,
    title: str,
    generated_at: str,
    alert_state: str,
    rows: list,
    csv_filename: str,
    get_dependabot: Callable,
    get_code_scanning: Callable,
    get_secret_scanning: Callable,
    get_renovate: Callable,
    get_total: Callable,
    issue_count: int,
    csv_is_detailed: bool = True,
) -> str:
    """Build Confluence storage-format HTML listing every repository."""

    dep_c = sum(get_dependabot(r).critical for r in rows)
    dep_h = sum(get_dependabot(r).high for r in rows)
    dep_m = sum(get_dependabot(r).medium for r in rows)
    dep_l = sum(get_dependabot(r).low for r in rows)
    dep_u = sum(get_dependabot(r).unknown for r in rows)
    dep_t = dep_c + dep_h + dep_m + dep_l + dep_u
    cs_c = sum(get_code_scanning(r).critical for r in rows)
    cs_h = sum(get_code_scanning(r).high for r in rows)
    cs_m = sum(get_code_scanning(r).medium for r in rows)
    cs_l = sum(get_code_scanning(r).low for r in rows)
    cs_u = sum(get_code_scanning(r).unknown for r in rows)
    cs_t = cs_c + cs_h + cs_m + cs_l + cs_u
    secrets_t = sum(get_secret_scanning(r) for r in rows)
    renovate_t = sum(get_renovate(r) for r in rows)
    grand_t = sum(get_total(r) for r in rows)

    summary = _table(
        _th("Origine", "Critica", "Alta", "Media", "Bassa", "Sconosciuta", "Totale"),
        [
            _td_html([
                "Dependabot",
                _count_cell(dep_c, "Red"),
                _count_cell(dep_h, "Yellow"),
                _count_cell(dep_m, "Yellow"),
                _count_cell(dep_l, "Green"),
                _count_cell(dep_u, "Grey"),
                _count_cell(dep_t),
            ]),
            _td_html([
                "Code scanning",
                _count_cell(cs_c, "Red"),
                _count_cell(cs_h, "Yellow"),
                _count_cell(cs_m, "Yellow"),
                _count_cell(cs_l, "Green"),
                _count_cell(cs_u, "Grey"),
                _count_cell(cs_t),
            ]),
            _td_html([
                "Secrets scanning",
                "—", "—", "—", "—", "—",
                _count_cell(secrets_t, "Red" if secrets_t else None),
            ]),
            _td_html([
                "PR Renovate",
                "—", "—", "—", "—", "—",
                _count_cell(renovate_t, "Yellow" if renovate_t else None),
            ]),
            _td_html(["TOTALE", "—", "—", "—", "—", "—", f"<strong>{grand_t}</strong>"]),
        ],
    )

    component_rows = []
    for row in rows:
        dep = get_dependabot(row)
        cs = get_code_scanning(row)
        secrets_n = get_secret_scanning(row)
        renovate_n = get_renovate(row)
        component_rows.append(_td_html([
            _repo_link(row.repository),
            _count_with_link(
                dep.total,
                "Red" if dep.critical or dep.high else None,
                _github_path_url(row.repository, "security/dependabot"),
            ),
            _count_with_link(
                cs.total,
                "Red" if cs.critical or cs.high else None,
                _github_path_url(row.repository, "security/code-scanning"),
            ),
            _count_with_link(
                secrets_n,
                "Red" if secrets_n else None,
                _github_path_url(row.repository, "security/secret-scanning"),
            ),
            _count_with_link(
                renovate_n,
                "Yellow" if renovate_n else None,
                _github_path_url(row.repository, "pulls"),
            ),
            _count_cell(get_total(row)),
        ]))
    component_rows.append(_td_html([
        "<strong>TOTALE</strong>",
        f"<strong>{dep_t}</strong>",
        f"<strong>{cs_t}</strong>",
        f"<strong>{secrets_t}</strong>",
        f"<strong>{renovate_t}</strong>",
        f"<strong>{grand_t}</strong>",
    ]))
    components = _table(
        _th("Repository", "Dependabot", "Code scanning", "Secrets", "PR Renovate", "Totale"),
        component_rows,
    )

    def _severity_table(get_counts: Callable, github_path: str) -> str:
        body = []
        tot_c = tot_h = tot_m = tot_l = tot_u = 0
        for row in rows:
            counts = get_counts(row)
            tot_c += counts.critical
            tot_h += counts.high
            tot_m += counts.medium
            tot_l += counts.low
            tot_u += counts.unknown
            body.append(_td_html([
                _repo_link(row.repository),
                _count_cell(counts.critical, "Red"),
                _count_cell(counts.high, "Yellow"),
                _count_cell(counts.medium, "Yellow"),
                _count_cell(counts.low, "Green"),
                _count_cell(counts.unknown, "Grey"),
                _count_with_link(
                    counts.total,
                    None,
                    _github_path_url(row.repository, github_path),
                ),
            ]))
        body.append(_td_html([
            "<strong>TOTALE</strong>",
            f"<strong>{tot_c}</strong>",
            f"<strong>{tot_h}</strong>",
            f"<strong>{tot_m}</strong>",
            f"<strong>{tot_l}</strong>",
            f"<strong>{tot_u}</strong>",
            f"<strong>{tot_c + tot_h + tot_m + tot_l + tot_u}</strong>",
        ]))
        return _table(
            _th("Repository", "Critica", "Alta", "Media", "Bassa", "Sconosciuta", "Totale"),
            body,
        )

    secrets_rows = []
    for row in rows:
        secrets_rows.append(_td_html([
            _repo_link(row.repository),
            _count_with_link(
                get_secret_scanning(row),
                "Red",
                _github_path_url(row.repository, "security/secret-scanning"),
            ),
            _count_with_link(
                get_renovate(row),
                "Yellow",
                _github_path_url(row.repository, "pulls"),
            ),
        ]))
    secrets_rows.append(_td_html([
        "<strong>TOTALE</strong>",
        f"<strong>{secrets_t}</strong>",
        f"<strong>{renovate_t}</strong>",
    ]))
    secrets_table = _table(
        _th("Repository", "Secrets esposti", "PR Renovate aperte"),
        secrets_rows,
    )

    stato = _ALERT_STATE_IT.get(alert_state, alert_state)
    csv_description = (
        "L'elenco completo delle segnalazioni è nel CSV allegato."
        if csv_is_detailed else "Il CSV allegato contiene i conteggi per repository, non le singole segnalazioni."
    )
    return f"""
<h1>{xml_escape(title)}</h1>
<p>Generato il <strong>{xml_escape(generated_at)}</strong> · Stato degli alert: <strong>{xml_escape(stato)}</strong> · Repository: <strong>{len(rows)}</strong> · Segnalazioni di dettaglio: <strong>{issue_count}</strong></p>
{_csv_download_block(csv_filename, csv_is_detailed)}
<ac:structured-macro ac:name="info">
  <ac:rich-text-body>
    <p>Questa pagina è uno snapshot statico di ogni componente. I conteggi diversi da zero includono un link <strong>apri</strong> verso GitHub. {csv_description}</p>
  </ac:rich-text-body>
</ac:structured-macro>
<ac:structured-macro ac:name="toc">
  <ac:parameter ac:name="maxLevel">2</ac:parameter>
</ac:structured-macro>
<h2>Riepilogo</h2>
{summary}
<h2>Tutti i componenti</h2>
<p>Sono inclusi tutti i repository dell'elenco di input, anche quelli senza segnalazioni. Accanto ai conteggi diversi da zero c'è un link <strong>apri</strong> alla vista GitHub corrispondente.</p>
{components}
<h2>Alert Dependabot</h2>
{_severity_table(get_dependabot, "security/dependabot")}
<h2>Alert Code scanning</h2>
{_severity_table(get_code_scanning, "security/code-scanning")}
<h2>Secrets scanning e PR Renovate</h2>
{secrets_table}
""".strip()


def publish_report(
    config: ConfluenceConfig,
    *,
    title: str,
    storage_html: str,
    csv_path: str,
    csv_filename: str,
) -> str:
    client = ConfluenceClient(config)
    page = client.upsert_page(title, storage_html)
    page_id = str(page.get("id") or "")
    if not page_id:
        raise RuntimeError("Confluence page id missing after upsert")
    client.attach_or_replace_file(page_id, csv_path, csv_filename)
    return client.page_url(page)
