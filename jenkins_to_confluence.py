# Copyright (c) 2010-2026 Nasuni Corporation, all rights reserved.

import argparse
import base64
import getpass
import html
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
import yaml
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

default_format = "%(asctime)s | %(filename)s:%(lineno)d | %(levelname)s | %(message)s"


default_level = logging.INFO


class Logger(object):

    def __init__(self, level=default_level, filename=os.devnull, filemode="a+", name=""):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)
        file_handler = logging.FileHandler(filename, mode=filemode, delay=True)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(default_format))
        self.logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(logging.Formatter(default_format))
        self.logger.addHandler(console_handler)

    def get_logger(self):
        return self.logger


logger = Logger(name="perf.utilities.logger").get_logger()


IMAGE_WIDTH = 401


CELL_WIDTHS = (315, 315, 315)


REQUEST_TIMEOUT = 60


class ConfluenceError(Exception):
    pass


def load_page_data(input_path):
    try:
        with input_path.open(encoding="utf-8") as input_file:
            data = json.load(input_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfluenceError(f"Unable to read JSON input {input_path}: {error}") from error

    if not isinstance(data, dict):
        raise ConfluenceError("The JSON root must be an object.")
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        raise ConfluenceError("The JSON input must include a non-empty title.")
    if len(data["title"]) > 255:
        raise ConfluenceError("The Confluence page title cannot exceed 255 characters.")
    if not isinstance(data.get("section_heading", "SMB"), str):
        raise ConfluenceError("section_heading must be text.")
    if not isinstance(data.get("tables"), list) or not data["tables"]:
        raise ConfluenceError("The JSON input must contain at least one table.")

    resolved_paths = {}
    for table_index, table in enumerate(data["tables"]):
        if not isinstance(table, dict) or not isinstance(table.get("cells"), list) or len(table["cells"]) != 3:
            raise ConfluenceError(f"Table {table_index + 1} must contain exactly three cells.")
        for cell_index, cell in enumerate(table["cells"]):
            if not isinstance(cell, dict):
                raise ConfluenceError(f"Table {table_index + 1}, cell {cell_index + 1} must be an object.")
            if not isinstance(cell.get("heading", ""), str):
                raise ConfluenceError(f"Table {table_index + 1}, cell {cell_index + 1} heading must be text.")
            caption_lines = cell.get("caption_lines", [])
            if not isinstance(caption_lines, list) or any(not isinstance(line, str) for line in caption_lines):
                raise ConfluenceError(
                    f"Table {table_index + 1}, cell {cell_index + 1} caption_lines must be a list of strings."
                )
            image = cell.get("image")
            if image is not None:
                if not isinstance(image, str) or not image:
                    raise ConfluenceError(f"Table {table_index + 1}, cell {cell_index + 1} image must be a path.")
                display_width = cell.get("display_width", IMAGE_WIDTH)
                if isinstance(display_width, bool) or not isinstance(display_width, int) or display_width < 1:
                    raise ConfluenceError(
                        f"Table {table_index + 1}, cell {cell_index + 1} display_width must be a positive integer."
                    )
                image_path = (input_path.parent / image).resolve()
                if not image_path.is_file():
                    raise ConfluenceError(f"Graph image does not exist: {image_path}")
                if not re.fullmatch(r"[A-Za-z0-9._ -]+", image_path.name):
                    raise ConfluenceError(
                        f"Graph image filename {image_path.name!r} contains unsupported characters; "
                        "use letters, digits, spaces, dots, underscores, and hyphens."
                    )
                resolved_paths[(table_index, cell_index)] = image_path

    data["_image_paths"] = resolved_paths
    return data


def validate_image_names(data):
    names = {}
    for image_path in data["_image_paths"].values():
        previous_path = names.get(image_path.name)
        if previous_path is not None and previous_path != image_path:
            raise ConfluenceError(
                f"Different graph files share the attachment name {image_path.name!r}; rename one of them."
            )
        names[image_path.name] = image_path


def storage_body(data, attachment_names):
    table_markup = []
    image_paths = data["_image_paths"]
    for table_index, table in enumerate(data["tables"]):
        columns = "".join(f'<col style="width: {width}px;" />' for width in CELL_WIDTHS)
        headings = "".join(
            f"<th><p><strong>{html.escape(cell.get('heading', ''))}</strong></p></th>" for cell in table["cells"]
        )

        image_cells = []
        caption_cells = []
        for cell_index, cell in enumerate(table["cells"]):
            image_path = image_paths.get((table_index, cell_index))
            if image_path is None:
                image_markup = "<p></p>"
            else:
                attachment_name = attachment_names.get(image_path)
                if attachment_name is None:
                    attachment_name = image_path.name
                image_markup = (
                    f'<ac:image ac:align="center" ac:width="{IMAGE_WIDTH}">'
                    f'<ri:attachment ri:filename="{html.escape(attachment_name, quote=True)}" />'
                    "</ac:image><p></p>"
                )
            image_cells.append(f"<td>{image_markup}</td>")

            caption = "<br />".join(html.escape(line) for line in cell.get("caption_lines", []))
            caption_cells.append(f"<td><p>{caption}</p></td>")

        table_markup.append(
            "<table>"
            f"<colgroup>{columns}</colgroup>"
            f"<thead><tr>{headings}</tr></thead>"
            "<tbody>"
            f"<tr>{''.join(image_cells)}</tr>"
            f"<tr>{''.join(caption_cells)}</tr>"
            "</tbody></table>"
        )

    heading = html.escape(data.get("section_heading", "SMB"))
    return f"<p><strong>{heading}</strong></p>" + "".join(table_markup) + "<p></p><p></p>"


class ConfluenceClient:

    def __init__(self, base_url, email, api_token):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith("https://"):
            raise ConfluenceError("The Confluence base URL must use HTTPS.")
        auth = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
        self.authorization = f"Basic {auth}"

    def request(self, method, endpoint, payload=None, headers=None):
        request_headers = {"Authorization": self.authorization, "Accept": "application/json"}
        request_body = None
        if payload is not None:
            request_headers["Content-Type"] = "application/json"
            request_body = json.dumps(payload).encode("utf-8")
        if headers:
            request_headers.update(headers)
        request = Request(self.base_url + endpoint, data=request_body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                response_body = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ConfluenceError(f"Confluence API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise ConfluenceError(f"Unable to reach Confluence: {error.reason}") from error
        if not response_body:
            return {}
        try:
            return json.loads(response_body.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ConfluenceError("Confluence returned an invalid JSON response.") from error

    def upload_attachment(self, page_id, image_path):
        boundary = f"----ConfluencePageGenerator{uuid.uuid4().hex}"
        filename = image_path.name
        file_bytes = image_path.read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("ascii"),
        ]
        endpoint = f"/rest/api/content/{quote(str(page_id), safe='')}/child/attachment"

        multipart_request = Request(
            self.base_url + endpoint,
            data=b"".join(parts),
            headers={
                "Authorization": self.authorization,
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Atlassian-Token": "no-check",
            },
            method="POST",
        )
        try:
            with urlopen(multipart_request, timeout=REQUEST_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ConfluenceError(f"Attachment upload failed for {filename}: HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise ConfluenceError(f"Attachment upload failed for {filename}: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ConfluenceError(f"Confluence returned invalid JSON after uploading {filename}.") from error
        attachments = result.get("results", [])
        if not attachments:
            raise ConfluenceError(f"Confluence did not confirm the upload of {filename}.")
        return attachments[0].get("title", filename)


def parse_destination(destination_url):
    if not isinstance(destination_url, str):
        raise ConfluenceError("Provide a Confluence folder URL as destination_url.")
    parsed = urlsplit(destination_url)
    match = re.fullmatch(r"/wiki/spaces/([^/]+)/folder/([0-9]+)(?:/[^/]*)?/?", parsed.path)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or not match:
        raise ConfluenceError(
            "destination_url must be an HTTPS Confluence folder link: "
            "https://your-site/wiki/spaces/SPACE/folder/ID/Folder-name"
        )
    return {
        "base_url": f"https://{parsed.netloc}/wiki",
        "space_key": unquote(match[1]),
        "folder_id": match[2],
    }


def validate_destination(client, title, destination_url):
    destination = parse_destination(destination_url)
    if client.base_url != destination["base_url"]:
        raise ConfluenceError("The destination folder must belong to the authenticated Confluence site.")
    folder_id = destination["folder_id"]
    folder = client.request("GET", f"/api/v2/folders/{folder_id}")
    space_id = str(folder.get("spaceId", ""))
    if str(folder.get("id")) != folder_id or folder.get("status") != "current" or not space_id.isdigit():
        raise ConfluenceError("The destination folder is unavailable or not current.")
    space = client.request("GET", f"/api/v2/spaces/{space_id}")
    if space.get("key") != destination["space_key"]:
        raise ConfluenceError("The destination URL's space does not match the folder's actual space.")
    query = urlencode({"space-id": space_id, "title": title, "status": "current", "limit": 1})
    existing = client.request("GET", f"/api/v2/pages?{query}")
    if existing.get("results"):
        raise ConfluenceError(
            f"A page titled {title!r} already exists in space {destination['space_key']!r}; "
            "choose another title or handle the existing page manually."
        )
    return {**destination, "space_id": space_id}


def create_page(client, data, destination_url):
    destination = validate_destination(client, data["title"], destination_url)
    created = client.request(
        "POST",
        "/api/v2/pages",
        {
            "status": "current",
            "title": data["title"],
            "spaceId": destination["space_id"],
            "parentId": destination["folder_id"],
            "body": {"value": "<p>Page generation in progress.</p>", "representation": "storage"},
        },
    )
    page_id = str(created["id"])
    logger.info("  Created page %s; uploading graph attachments", page_id)
    try:
        attachment_names = {}
        images = list(dict.fromkeys(data["_image_paths"].values()))
        for index, image_path in enumerate(images, 1):
            logger.info("  Upload [%s/%s] %s", index, len(images), image_path.name)
            attachment_names[image_path] = client.upload_attachment(page_id, image_path)
        body = storage_body(data, attachment_names)
        logger.info("  Saving page content")
        updated = client.request(
            "PUT",
            f"/api/v2/pages/{quote(page_id, safe='')}",
            {
                "id": page_id,
                "status": "current",
                "title": data["title"],
                "spaceId": destination["space_id"],
                "parentId": destination["folder_id"],
                "version": {"number": int(created["version"]["number"]) + 1},
                "body": {"value": body, "representation": "storage"},
            },
        )
        if (
            str(updated.get("parentId")) != destination["folder_id"]
            or str(updated.get("spaceId")) != destination["space_id"]
        ):
            raise ConfluenceError("Confluence did not confirm the requested folder placement.")
    except (ConfluenceError, KeyError, OSError) as error:
        raise ConfluenceError(
            f"Page {page_id} was created, but generation did not finish: {error}. "
            "The incomplete page was left in place for investigation."
        ) from error

    links = created.get("_links", {})
    page_url = links.get("base", client.base_url) + links.get("webui", f"/pages/viewpage.action?pageId={page_id}")
    return page_id, page_url


class PocError(Exception):
    pass


def configure_console():
    for handler in logger.handlers:
        if not hasattr(handler, "baseFilename") and handler.formatter is not None:
            handler.setFormatter(type(handler.formatter)("%(message)s"))


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port


class SameOriginRedirect(HTTPRedirectHandler):

    def redirect_request(self, request, response, code, message, headers, new_url):
        if origin(request.full_url) != origin(new_url):
            raise PocError("Jenkins redirected to another origin; refusing to forward credentials.")
        return super().redirect_request(request, response, code, message, headers, new_url)


class Jenkins:

    def __init__(self, base_url, username, token):
        self.base_url = base_url.rstrip("/") + "/"
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode("ascii")
        self.headers = {"Authorization": f"Basic {credentials}"}
        self._thread_state = threading.local()

    def url(self, path):
        url = urljoin(self.base_url, path)
        if origin(url) != origin(self.base_url):
            raise PocError("Build, queue, and artifact URLs must use the configured Jenkins origin.")
        return url

    def open(self, path, data=None, headers=None):
        request = Request(self.url(path), data=data, headers={**self.headers, **(headers or {})})

        if not hasattr(self._thread_state, "opener"):
            self._thread_state.opener = build_opener(SameOriginRedirect())
        try:
            return self._thread_state.opener.open(request, timeout=60)
        except HTTPError as error:
            raise PocError(
                f"Jenkins HTTP {error.code} for {request.full_url}. "
                "Check the URL, token, permissions, and any CSRF/reverse-proxy policy."
            ) from error
        except (URLError, TimeoutError) as error:
            raise PocError(f"Jenkins request failed for {request.full_url}: {error}") from error

    def get_json(self, path):
        with self.open(path) as response:
            try:
                return json.load(response)
            except (ValueError, UnicodeError) as error:
                raise PocError("Jenkins returned non-JSON content; check authentication and the endpoint.") from error

    def download(self, path, destination):
        if destination.exists():
            raise PocError(f"Refusing to overwrite {destination}")
        temporary = destination.with_name(destination.name + ".part")
        started = time.monotonic()
        logger.info("  Downloading %s", destination.name)
        try:
            with self.open(path) as response, temporary.open("xb") as output:
                downloaded = 0
                last_update = started
                headers = getattr(response, "headers", {})
                length = headers.get("Content-Length", "")
                total = int(length) if length.isdigit() else 0
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_update >= 5:
                        progress = f"{downloaded / total:.0%}" if total else "in progress"
                        logger.info("    %s | %.1f MiB | %s", destination.name, downloaded / 1048576, progress)
                        last_update = now
            temporary.rename(destination)
        finally:
            temporary.unlink(missing_ok=True)
        logger.info(
            "  Saved %s | %.1f MiB | %.1fs",
            destination.name,
            destination.stat().st_size / 1048576,
            time.monotonic() - started,
        )
        logger.debug("Downloaded artifact path: %s", destination)


def build_metadata(client, build_url):
    query = urlencode({"tree": "number,url,building,result,artifacts[fileName,relativePath]"})
    return client.get_json(build_url.rstrip("/") + "/api/json?" + query)


def artifact_url(build_url, relative_path):
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise PocError(f"Unsafe Jenkins artifact path: {relative_path}")
    return build_url.rstrip("/") + "/artifact/" + quote(relative_path, safe="/")


def parse_summary(text):
    executions = []
    current = None
    fields = {"TestCase": "testcase", "UUID": "uuid", "Baseline UUID": "baseline_uuid", "Status": "status"}
    for line in text.splitlines():
        label, separator, value = line.partition(" - ")
        if not separator:
            continue
        label = label.strip()
        if label == "TestCase":
            current = {}
            executions.append(current)
        if current is not None and label in fields:
            current[fields[label]] = value.strip()
    if not executions:
        raise PocError("No test cases found in test_summary.txt; its format may differ from this framework.")
    return executions


def unique_artifact(artifacts, filename):
    matches = [artifact for artifact in artifacts if artifact["fileName"] == filename]
    if len(matches) != 1:
        raise PocError(f"Expected one artifact named {filename!r}, found {len(matches)}.")
    return matches[0]


def extract_graphs(archive_path, destination):
    with zipfile.ZipFile(archive_path) as archive:
        members = [
            member for member in archive.infolist() if not member.is_dir() and member.filename.lower().endswith(".png")
        ]
        if not members:
            raise PocError(f"No PNG graphs found in {archive_path.name}.")
        names = [PurePosixPath(member.filename).name for member in members]
        if len(set(names)) != len(names):
            raise PocError(f"Ambiguous duplicate PNG basenames in {archive_path.name}.")
        destination.mkdir()
        for member, name in zip(members, names):
            with archive.open(member) as source, (destination / name).open("xb") as output:
                shutil.copyfileobj(source, output)
    return [destination / name for name in names]


def wait_for_comparison(client, queue_url, timeout, interval):
    started = time.monotonic()
    deadline = started + timeout
    last_update = started
    logger.info("  Queued: %s", queue_url)
    build_url = None
    while time.monotonic() < deadline:
        if build_url is None:
            queued = client.get_json(queue_url.rstrip("/") + "/api/json")
            if queued.get("cancelled"):
                raise PocError(f"Comparison queue item was cancelled: {queue_url}")
            build_url = (queued.get("executable") or {}).get("url")
            if build_url:
                logger.info("  Running: %s", build_url)
        if build_url:
            metadata = build_metadata(client, build_url)
            if not metadata["building"] and metadata["result"] is not None:
                if metadata["result"] != "SUCCESS":
                    raise PocError(f"Comparison build ended with {metadata['result']}: {build_url}")
                logger.info("  Comparison finished | %.0fs", time.monotonic() - started)
                return metadata
        now = time.monotonic()
        if now - last_update >= 30:
            logger.info(
                "  Still %s | %.0fs elapsed | %s",
                "running" if build_url else "queued",
                now - started,
                build_url or queue_url,
            )
            last_update = now
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise PocError(
        f"Timed out waiting for {build_url or queue_url}. The Jenkins job was NOT cancelled; "
        "check this URL before triggering another comparison."
    )


def validate_graph_job(client, job_url, axes):
    job_url = client.url(job_url).rstrip("/") + "/"
    job = client.get_json(job_url + "api/json")
    definitions = {
        definition["name"]: definition
        for property_item in job.get("property", [])
        for definition in property_item.get("parameterDefinitions", [])
    }
    parameters = {"UUIDs", "Axes", "Title", "DESCRIPTION"}
    if not parameters.issubset(definitions):
        raise PocError(f"Graph job does not declare the expected parameters: {sorted(parameters)}")
    choices = definitions["Axes"].get("choices")
    if choices is not None and axes not in choices:
        raise PocError(f"The graph job does not offer {axes}.")


def run_comparison(client, args, output):
    job_url = client.url(args.graph_job_url).rstrip("/") + "/"
    axes = getattr(args, "axes", "ach_rate_vs_lat")
    parameters = {
        "UUIDs": f"{args.current_uuid},{args.baseline_uuid}",
        "Axes": axes,
        "Title": args.title,
        "DESCRIPTION": args.description,
    }
    validate_graph_job(client, job_url, axes)

    with client.open(
        job_url + "buildWithParameters",
        data=urlencode(parameters).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    ) as response:
        location = response.headers.get("Location")
        if response.status != 201 or not location:
            raise PocError(
                "Jenkins did not confirm a queued build (HTTP 201 + Location); inspect the job before retrying."
            )
        queue_url = client.url(urljoin(job_url, location))
    (output / "queue.json").write_text(
        json.dumps({"queue_url": queue_url, "parameters": parameters}, indent=2) + "\n", encoding="utf-8"
    )
    metadata = wait_for_comparison(client, queue_url, args.timeout, args.poll_interval)
    graphs = [artifact for artifact in metadata["artifacts"] if artifact["fileName"].lower().endswith(".png")]
    if not graphs:
        raise PocError(f"The successful comparison build has no archived PNG: {metadata['url']}")
    if len({graph["fileName"] for graph in graphs}) != len(graphs):
        raise PocError("Comparison artifacts have duplicate PNG basenames; explicit selection is required.")
    for graph in graphs:
        if "/" in graph["fileName"] or "\\" in graph["fileName"]:
            raise PocError(f"Unsafe comparison artifact filename: {graph['fileName']!r}")
        client.download(artifact_url(metadata["url"], graph["relativePath"]), output / graph["fileName"])
    return {
        "queue_url": queue_url,
        "build_url": metadata["url"],
        "parameters": parameters,
        "graphs": [graph["fileName"] for graph in graphs],
    }


class ReportLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise PocError("YAML mapping keys must be strings.")
        if key in result:
            raise PocError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


ReportLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise PocError(f"{label} must be a non-empty string.")
    return value


def check_keys(mapping, allowed, label):
    if not isinstance(mapping, dict):
        raise PocError(f"{label} must be a mapping.")
    unknown = set(mapping) - set(allowed)
    if unknown:
        raise PocError(f"Unknown fields in {label}: {sorted(unknown)}")


def load_config(path):
    with path.open(encoding="utf-8") as source:
        config = yaml.load(source, Loader=ReportLoader)
    check_keys(
        config,
        (
            "build_url",
            "system",
            "capability",
            "protocol",
            "confluence",
            "graphing",
            "testsuites",
            "allow_failed_results",
        ),
        "report",
    )
    for field in ("build_url", "system", "protocol"):
        require_text(config.get(field), field)
    config["capability"] = require_text(config.get("capability", config["system"]), "capability")
    parsed = urlsplit(config["build_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise PocError("build_url must be an HTTP(S) URL without embedded credentials.")
    if parsed.query or parsed.fragment or not parsed.path.rstrip("/").split("/")[-1].isdigit():
        raise PocError("build_url must identify a numbered build, without a query or fragment.")
    if not isinstance(config.get("allow_failed_results", False), bool):
        raise PocError("allow_failed_results must be true or false.")
    confluence = config.get("confluence")
    check_keys(confluence, ("title", "destination_url"), "confluence")
    require_text(confluence.get("title"), "confluence.title")
    if len(confluence["title"]) > 255:
        raise PocError("confluence.title cannot exceed 255 characters.")
    parse_destination(confluence.get("destination_url"))
    graphing = config.setdefault("graphing", {})
    check_keys(graphing, ("job_url", "axes"), "graphing")
    require_text(graphing.get("axes", "ach_rate_vs_lat"), "graphing.axes")
    if "job_url" in graphing:
        require_text(graphing["job_url"], "graphing.job_url")
    suites = config.get("testsuites")
    if not isinstance(suites, list) or not suites:
        raise PocError("testsuites must be a non-empty list.")
    cases = []
    for suite in suites:
        check_keys(suite, ("name", "testcases"), "testsuite")
        require_text(suite.get("name"), "testsuite.name")
        if not isinstance(suite.get("testcases"), list) or not suite["testcases"]:
            raise PocError("Each testsuite needs a non-empty testcases list.")
        for case in suite["testcases"]:
            check_keys(case, ("name", "execution_name", "heading", "baseline", "axes"), "testcase")
            require_text(case.get("name"), "testcase.name")
            for field in ("execution_name", "heading", "axes"):
                if field in case:
                    require_text(case[field], f"testcase.{field}")
            baseline = case.get("baseline")
            if baseline is not None:
                require_text(baseline, "testcase.baseline")
                if baseline.strip().lower() in ("none", "null", "na", "n/a") or "," in baseline:
                    raise PocError("baseline must be a single UUID, or YAML null/omitted for current-only.")
            cases.append(
                {
                    **case,
                    "suite": suite["name"],
                    "axes": case.get("axes", graphing.get("axes", "ach_rate_vs_lat")),
                    "heading": case.get("heading", case["name"].replace("_", " ")),
                }
            )
    config["_cases"] = cases
    return config


def resolve_cases(config, executions):
    resolved = []
    used = set()
    for case in config["_cases"]:
        if "execution_name" in case:
            expected = case["execution_name"]
        elif case["suite"] == "spec/spec.py":
            expected = f"{config['protocol']}_{config['capability']}_{case['name']}"
        else:
            expected = case["name"]
        matches = [execution for execution in executions if execution.get("testcase", "").upper() == expected.upper()]
        if len(matches) != 1:
            available = [execution.get("testcase") for execution in executions]
            raise PocError(
                f"{case['suite']}:{case['name']} resolved to {expected!r}, with {len(matches)} matches. "
                f"Set execution_name to an exact summary name. Available: {available}"
            )
        execution = matches[0]
        current_uuid = require_text(execution.get("uuid"), f"UUID for {expected}")
        if current_uuid.lower() in ("na", "n/a", "none") or any(char in current_uuid for char in "/\\,"):
            raise PocError(f"No usable current execution UUID for {expected}: {current_uuid}")
        if current_uuid in (".", "..") or current_uuid in used:
            raise PocError(f"Duplicate or invalid execution UUID selected: {current_uuid}")
        used.add(current_uuid)
        status = execution.get("status", "UNKNOWN")
        if status.upper() != "PASSED" and not config.get("allow_failed_results", False):
            raise PocError(f"{expected} status is {status}. Set allow_failed_results: true to report it explicitly.")
        if case.get("baseline") == current_uuid:
            raise PocError(f"Current and baseline UUIDs are identical for {expected}.")
        resolved.append(
            {
                **case,
                "runtime_testcase": execution["testcase"],
                "uuid": current_uuid,
                "status": status,
                "recorded_baseline_uuid": execution.get("baseline_uuid"),
            }
        )
    return resolved


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def prepare_report(client, config, output, workers=4, timeout=1800, poll_interval=5):
    if workers < 1 or timeout < 1 or poll_interval < 1:
        raise PocError("Workers, timeout, and polling interval must be positive.")
    logger.info("\n[1/3] Resolve test cases\n  Build: %s", config["build_url"])
    metadata = build_metadata(client, config["build_url"])
    if metadata["building"] or metadata["result"] is None:
        raise PocError("The source build has not finished.")
    logger.info("Source build result: %s", metadata["result"])
    summary = unique_artifact(metadata["artifacts"], "test_summary.txt")
    client.download(artifact_url(config["build_url"], summary["relativePath"]), output / "test_summary.txt")
    executions = parse_summary((output / "test_summary.txt").read_text(encoding="utf-8"))
    cases = resolve_cases(config, executions)
    job_url = config["graphing"].get("job_url", "job/Automation_Graphing/")
    for index, case in enumerate(cases, 1):
        if case.get("baseline"):
            validate_graph_job(client, job_url, case["axes"])
        else:
            case["artifact"] = unique_artifact(metadata["artifacts"], case["uuid"] + ".zip")["relativePath"]
        logger.info(
            "  %02d. %-35s %s | %s",
            index,
            case["heading"],
            f"comparison ({case['axes']})" if case.get("baseline") else "current graph",
            case["status"],
        )
    save_json(output / "plan.json", {"build_url": config["build_url"], "cases": cases})
    (output / "graphs").mkdir()
    comparisons = sum(bool(case.get("baseline")) for case in cases)
    logger.info(
        "\n[2/3] Collect graphs\n  %s cases | %s comparisons | %s current graphs | %s download workers",
        len(cases),
        comparisons,
        len(cases) - comparisons,
        workers,
    )
    completed = 0
    progress_lock = threading.Lock()

    def obtain_graph(item):
        index, case = item
        nonlocal completed
        started = time.monotonic()
        label = f"Case {index + 1:02d}/{len(cases):02d} - {case['heading']}"
        logger.info("\n  %s | %s", label, "generating comparison" if case.get("baseline") else "fetching current graph")
        work = output / f"case-{index + 1:02d}"
        work.mkdir()
        if case.get("baseline"):
            args = SimpleNamespace(
                graph_job_url=job_url,
                axes=case["axes"],
                current_uuid=case["uuid"],
                baseline_uuid=case["baseline"],
                title=f"{config['protocol']} {case['heading']} - Current vs Baseline",
                description=f"Performance report from {config['build_url']}; test {case['runtime_testcase']}.",
                timeout=timeout,
                poll_interval=poll_interval,
            )
            comparison = run_comparison(client, args, work)
            graph_paths = [work / graph for graph in comparison["graphs"]]
            source = {"type": "comparison", **comparison}
        else:
            archive = work / (case["uuid"] + ".zip")
            client.download(artifact_url(config["build_url"], case["artifact"]), archive)
            graph_paths = extract_graphs(archive, work / "extracted")
            source = {"type": "execution", "artifact": case["artifact"]}
        standard_graphs = [path for path in graph_paths if not path.stem.lower().endswith("_(log)")]
        if len(standard_graphs) != 1:
            raise PocError(
                f"{case['runtime_testcase']} produced {len(standard_graphs)} PNGs without the _(log) suffix; "
                "expected exactly one."
            )
        graph = output / "graphs" / f"{index + 1:02d}-graph.png"
        shutil.copyfile(standard_graphs[0], graph)
        with progress_lock:
            completed += 1
            logger.info(
                "  [%s/%s ready] %s | %.1fs | %s",
                completed,
                len(cases),
                case["heading"],
                time.monotonic() - started,
                graph.relative_to(output),
            )
        return {**case, "graph": graph.relative_to(output).as_posix(), "graph_source": source}

    results = [None] * len(cases)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            index: executor.submit(obtain_graph, (index, case))
            for index, case in enumerate(cases)
            if not case.get("baseline")
        }
        try:
            for index, case in enumerate(cases):
                results[index] = obtain_graph((index, case)) if case.get("baseline") else futures[index].result()
        finally:
            for future in futures.values():
                future.cancel()
    save_json(
        output / "resolved-manifest.json",
        {"build_url": config["build_url"], "build_result": metadata["result"], "executions": results},
    )
    cells = []
    for index, case in enumerate(results):
        captions = [case["uuid"]]
        if case.get("baseline"):
            captions.append("Baseline: " + case["baseline"])
        cells.append(
            {
                "heading": case["heading"],
                "image": case["graph"],
                "display_width": IMAGE_WIDTH,
                "caption_lines": captions,
            }
        )
    while len(cells) % 3:
        cells.append({"heading": "", "image": None, "caption_lines": []})
    page = {
        "title": config["confluence"]["title"],
        "destination_url": config["confluence"]["destination_url"],
        "section_heading": config["protocol"],
        "tables": [{"cells": cells[index : index + 3]} for index in range(0, len(cells), 3)],
    }
    save_json(output / "page.json", page)
    data = load_page_data(output / "page.json")
    validate_image_names(data)
    return data


def parse_args():
    parser = argparse.ArgumentParser(description="Build a Confluence performance report from YAML.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory for the report bundle.")
    parser.add_argument("--prepare-only", action="store_true", help="Obtain graphs but do not access Confluence.")
    parser.add_argument("--allow-http", action="store_true", help="Allow unencrypted Jenkins token authentication.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent current-only ZIP downloads.")
    parser.add_argument("--timeout", type=int, default=1800, help="Wait limit per comparison job in seconds.")
    parser.add_argument("--poll-interval", type=int, default=5)
    return parser.parse_args()


def main():
    args = parse_args()
    configure_console()
    started = time.monotonic()
    try:
        config = load_config(args.config)
        logger.info(
            "\nPerformance report\n==================\n  Title:  %s\n  Mode:   %s\n  Output: %s",
            config["confluence"]["title"],
            "prepare only" if args.prepare_only else "prepare and publish",
            args.output.resolve(),
        )
        if min(args.workers, args.timeout, args.poll_interval) < 1:
            raise PocError("Workers, timeout, and polling interval must be positive.")
        if args.output.exists() or args.output.is_symlink():
            raise PocError(f"Choose a fresh output directory: {args.output}")
        parsed = urlsplit(config["build_url"])
        if parsed.scheme == "http" and not args.allow_http:
            raise PocError("Jenkins uses HTTP. Use --allow-http only on an approved internal network.")
        base_url = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        user = os.environ.get("JENKINS_USER") or input("Jenkins username: ").strip()
        token = os.environ.get("JENKINS_API_TOKEN") or getpass.getpass("Jenkins API token (hidden): ")
        if not user or not token:
            raise PocError("Jenkins username and API token are required.")
        client = Jenkins(base_url, user, token)
        if not client.get_json("whoAmI/api/json").get("authenticated"):
            raise PocError("Jenkins authentication failed.")
        logger.info("  Jenkins authentication: OK")
        publisher = None
        if not args.prepare_only:
            email = os.environ.get("CONFLUENCE_EMAIL") or input("Confluence email: ").strip()
            api_token = os.environ.get("CONFLUENCE_API_TOKEN") or getpass.getpass("Confluence API token (hidden): ")
            if not email or not api_token:
                raise PocError("Confluence email and API token are required.")
            publisher = ConfluenceClient(
                parse_destination(config["confluence"]["destination_url"])["base_url"],
                email,
                api_token,
            )
            validate_destination(publisher, config["confluence"]["title"], config["confluence"]["destination_url"])
            logger.info("  Confluence destination: OK")
        args.output.mkdir(parents=True)
        data = prepare_report(client, config, args.output, args.workers, args.timeout, args.poll_interval)
        logger.info("Prepared publisher input: %s", (args.output / "page.json").resolve())
        if publisher is not None:
            logger.info("\n[3/3] Publish to Confluence")
            page_id, page_url = create_page(publisher, data, config["confluence"]["destination_url"])
            save_json(args.output / "publication.json", {"page_id": page_id, "page_url": page_url})
            logger.info("Published report: %s", page_url)
            logger.info("Removing local report files: %s", args.output.resolve())
            try:
                shutil.rmtree(args.output)
            except OSError as error:
                logger.error(
                    "Report was published successfully at %s, but local cleanup failed for %s: %s. "
                    "Remove the remaining files manually; do not republish.",
                    page_url,
                    args.output,
                    error,
                )
                return 1
            logger.info("Local report files removed.")
        else:
            logger.info("\n[3/3] Ready for review - Confluence publication skipped")
            logger.info("  Report bundle: %s", args.output.resolve())
        logger.info("\nDone in %.1fs", time.monotonic() - started)
        return 0
    except (
        PocError,
        ConfluenceError,
        OSError,
        ValueError,
        KeyError,
        yaml.YAMLError,
        zipfile.BadZipFile,
        EOFError,
    ) as error:
        logger.error("%s", error)
        logger.error(
            "Inspect %s for the plan, partial downloads, and comparison queue URLs. "
            "No jobs are cancelled automatically; check them before retrying.",
            args.output,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
