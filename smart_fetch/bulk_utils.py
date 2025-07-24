"""Support for FHIR bulk exports"""

import asyncio
import datetime
import gzip
import json
import logging
import os
import sys
import typing
import urllib.parse
import uuid
from collections.abc import Callable
from functools import partial

import cumulus_fhir_support as cfs
import httpx
import rich.live
import rich.text

import smart_fetch
from smart_fetch import cli_utils, filtering, lifecycle, merges, ndjson, resources, timing


def export_url(fhir_url: str, group: str) -> str:
    if group:
        return os.path.join(fhir_url, "Group", group)
    else:
        return fhir_url


class BulkExportLogWriter:
    """Writes a standard log bulk export file."""

    def __init__(self, root: str):
        # Start with a random export ID, which will be used if we fail to even kick off an export.
        # But this will normally be overridden by the poll location for a consistent exportId
        # across interrupted exports and their restarts.
        self.export_id = str(uuid.uuid4())
        self._filename = os.path.join(root, "log.ndjson")
        self._num_files = 0
        self._num_resources = 0
        self._num_bytes = 0
        self._start_time = None

    def _event(
        self, event_id: str, detail: dict, *, timestamp: datetime.datetime | None = None
    ) -> None:
        timestamp = timestamp or timing.now()
        if self._start_time is None:
            self._start_time = timestamp

        # We open the file anew for each event because:
        # a) logging should be flushed often to disk
        # b) it makes the API of the class easier by avoiding a context manager
        with open(self._filename, "a", encoding="utf8") as f:
            row = {
                "exportId": self.export_id,
                "timestamp": timestamp.isoformat(),
                "eventId": event_id,
                "eventDetail": detail,
            }
            if event_id == "kickoff":
                # The bulk logging spec says we can add whatever other keys we want,
                # but does not encourage a namespace to separate them or anything.
                # We use a sunder prefix, just in case the spec wants to add new keys itself.
                row["_client"] = "smart-fetch"
                row["_clientVersion"] = smart_fetch.__version__
            json.dump(row, f)
            f.write("\n")

    @staticmethod
    def _body(response: httpx.Response) -> dict | str:
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass  # fall back to text
        return response.text

    @staticmethod
    def _response_info(response: httpx.Response) -> dict:
        return {
            "body": BulkExportLogWriter._body(response),
            "code": response.status_code,
            "responseHeaders": dict(response.headers),
        }

    @staticmethod
    def _error_info(exc: Exception) -> dict:
        """Merge the returned dictionary into an event detail object"""
        info = {
            "body": None,
            "code": None,
            "message": str(exc),
            "responseHeaders": None,
        }

        if isinstance(exc, cfs.NetworkError) and exc.response:
            info.update(BulkExportLogWriter._response_info(exc.response))

        return info

    def kickoff(self, url: str, capabilities: dict, response: httpx.Response | Exception):
        # https://www.hl7.org/fhir/R4/capabilitystatement.html
        software = capabilities.get("software", {})

        # Create a "merged" version of the params.
        # (Merged in the sense that duplicates are converted to comma separated lists.)
        request_headers = {}
        for k, v in httpx.URL(url).params.multi_items():
            if k in request_headers:
                # We currently don't hit this path, since we don't allow duplicates in headers
                # right now. But we may in the future (and must, with future versions of the bulk
                # spec), so leave this here, dormant.
                request_headers[k] += f",{v}"  # pragma: no cover
            else:
                request_headers[k] = v
        # Spec says we shouldn't log the `patient` parameter, so strip it here.
        request_headers.pop("patient", None)

        if isinstance(response, Exception):
            response_info = self._error_info(response)
            if response_info["body"] is None:  # for non-httpx error cases
                response_info["body"] = response_info["message"]
        else:
            response_info = BulkExportLogWriter._response_info(response)
            if response.status_code == 202:
                response_info["body"] = None
                response_info["code"] = None

        self._event(
            "kickoff",
            {
                "exportUrl": url,
                "softwareName": software.get("name"),
                "softwareVersion": software.get("version"),
                "softwareReleaseDate": software.get("releaseDate"),
                "fhirVersion": capabilities.get("fhirVersion"),
                "requestParameters": request_headers,
                "errorCode": response_info["code"],
                "errorBody": response_info["body"],
                "responseHeaders": response_info["responseHeaders"],
            },
        )

    def status_complete(self, response: httpx.Response):
        response_json = response.json()
        transaction_time = response_json.get("transactionTime")
        num_output = len(response_json.get("output", []))
        num_deleted = len(response_json.get("deleted", []))
        num_errors = len(response_json.get("error", []))

        # The log format makes a distinction between complete manifests and the whole process
        # being complete. But since we as a client don't yet support manifests, we'll just treat
        # them the same and mark everything complete all at once here.
        self._event(
            "status_complete",
            {
                "transactionTime": transaction_time,
            },
        )
        self._event(
            "status_page_complete",
            {
                "transactionTime": transaction_time,
                "outputFileCount": num_output,
                "deletedFileCount": num_deleted,
                "errorFileCount": num_errors,
            },
        )
        self._event(
            "manifest_complete",
            {
                "transactionTime": transaction_time,
                "totalOutputFileCount": num_output,
                "totalDeletedFileCount": num_deleted,
                "totalErrorFileCount": num_errors,
                "totalManifests": 1,
            },
        )

    def status_error(self, exc: Exception):
        self._event(
            "status_error",
            self._error_info(exc),
        )

    def download_request(
        self,
        file_url: str | httpx.URL,
        item_type: str,
        resource_type: str | None,
    ):
        self._event(
            "download_request",
            {
                "fileUrl": str(file_url),
                "itemType": item_type,
                "resourceType": resource_type,
            },
        )

    def download_complete(
        self,
        file_url: str | httpx.URL,
        resource_count: int | None,
        file_size: int,
    ):
        self._num_files += 1
        self._num_resources += resource_count or 0
        self._num_bytes += file_size
        self._event(
            "download_complete",
            {
                "fileUrl": str(file_url),
                "resourceCount": resource_count,
                "fileSize": file_size,
            },
        )

    def download_error(self, file_url: str | httpx.URL, exc: Exception):
        self._event(
            "download_error",
            {
                "fileUrl": str(file_url),
                **self._error_info(exc),
            },
        )

    def export_complete(self):
        timestamp = timing.now()
        duration = (timestamp - self._start_time) if self._start_time else None
        self._event(
            "export_complete",
            {
                "files": self._num_files,
                "resources": self._num_resources,
                "bytes": self._num_bytes,
                "attachments": None,
                "duration": duration // datetime.timedelta(milliseconds=1) if duration else 0,
            },
            timestamp=timestamp,
        )


class BulkExporter:
    """
    Perform a bulk export from a FHIR server that supports the Backend Service SMART profile.

    This has been manually tested against at least:
    - The bulk-data-server test server (https://github.com/smart-on-fhir/bulk-data-server)
    - Oracle (https://docs.oracle.com/en/industries/health/millennium-platform-apis/mfbda/index.html)
    - Epic (https://fhir.epic.com/Documentation?docId=fhir_bulk_data)
    """

    _TIMEOUT_THRESHOLD = 60 * 60 * 24 * 30  # thirty days (we've seen some multi-week Epic waits)

    def __init__(
        self,
        client: cfs.FhirClient,
        resources: set[str],
        url: str,
        destination: str,
        *,
        since: str | None = None,
        type_filter: filtering.TypeFilters | None = None,
        metadata: lifecycle.OutputMetadata,
        prefer_url_resources: bool = False,
    ):
        """
        Initialize a bulk exporter (but does not start an export).

        :param client: a client ready to make requests
        :param resources: a list of resource names to export
        :param url: a target export URL (like https://example.com/Group/1234)
        :param destination: a local folder to store all the files
        :param since: start date for export
        :param type_filter: search filter for export (_typeFilter)
        :param metadata: metadata object to pull resume information from
        :param prefer_url_resources: if the URL includes _type, ignore the provided resources
        """
        super().__init__()
        self._client = client
        self._destination = destination
        self._total_wait_time = 0  # in seconds, across all our requests
        self._log: BulkExportLogWriter | None = None
        self._metadata = metadata

        self.export_url = self._format_kickoff_url(
            url,
            server_type=client.server_type,
            resources=resources,
            since=since,
            type_filter=type_filter,
            prefer_url_resources=prefer_url_resources,
        )

        # Will be filled out by export()
        self.transaction_time: datetime.datetime | None = None

    @staticmethod
    def combine_filters(
        filters: filtering.TypeFilters | None, *, resources: set[str], server_type: cfs.ServerType
    ) -> list[str]:
        if filters is None:
            return []

        # The text of the spec is a little vague about how exactly to quote/escape type filters.
        #
        # Here's the one example from https://hl7.org/fhir/uv/bulkdata/export.html#example-request:
        # $export?
        #   _type=
        #     MedicationRequest,
        #     Condition&
        #   _typeFilter=
        #     MedicationRequest%3Fstatus%3Dactive,
        #     MedicationRequest%3Fstatus%3Dcompleted%26date%3Dgt2018-07-01T00%3A00%3A00Z
        #
        # OK, that's fine. But what about commas, which now have three uses?
        # 1) Separating multiple type filters (like above)
        # 2) Separating filter values (Encounter?status=finished,unknown)
        # 3) Internal to filter values (Encounter?status=internal\,comma)
        #
        # We now need a new layer of quoting to separate level 1 from level 2 (and 3).
        # Is that another level of URL-quoting? For the whole filter or just commas?
        # Or is it backslash escaping like how FHIR searches disambiguate 2 and 3 above normally?
        # The spec is not explicit.
        #
        # And thus implementers may differ in how to quote it.
        # Most seem to support URL-quoting just the inside commas an extra time. (and some don't
        # like URL-quoting everything an extra time, just the comma)
        # Epic's documentation suggests that it wants you to backslash escape the comma.
        # And that does work like you want. But so does URL-quoting it!
        # Since URL-quoting-the-comma has broad support, we'll use that by default.
        # If we hit servers that can only handle a backslash version, we can add that below.
        # Servers known to support URL-quoted-commas: Epic, Hapi, Kodjin

        # if server_type in {...}:
        #     def quote(type_filter: str) -> str:
        #         return type_filter.replace("\\", "\\\\").replace(",", "\\,")

        def quote(type_filter: str) -> str:
            return type_filter.replace(",", "%2C")

        return [
            quote(f"{res_type}?{single_filter}")
            for res_type in sorted(resources)
            for single_filter in sorted(filters[res_type])
        ]

    @staticmethod
    def _format_kickoff_url(
        url: str,
        *,
        server_type: cfs.ServerType,
        resources: set[str],
        since: str | None,
        type_filter: filtering.TypeFilters | None,
        prefer_url_resources: bool,
    ) -> str:
        parsed = urllib.parse.urlsplit(url)

        # Add an export path to the end of the URL if it's not provided
        if not parsed.path.endswith("/$export"):
            parsed = parsed._replace(path=os.path.join(parsed.path, "$export"))

        # Integrate in any externally-provided flags
        query = urllib.parse.parse_qs(parsed.query)
        ignore_provided_resources = prefer_url_resources and "_type" in query
        if not ignore_provided_resources:
            query.setdefault("_type", []).extend(sorted(resources))
        combined_filters = BulkExporter.combine_filters(
            type_filter, resources=resources, server_type=server_type
        )
        if combined_filters:
            query.setdefault("_typeFilter", []).extend(combined_filters)
        if since:
            query["_since"] = [since]

        # Combine repeated query params into a single comma-delimited params.
        # The spec says servers SHALL support repeated params (and even prefers them, claiming
        # that comma-delimited params may be deprecated in future releases).
        # But... Cerner doesn't support repeated _type at least.
        # So for wider compatibility, we'll condense into comma-delimited params.
        query = {k: ",".join(v) if isinstance(v, list) else v for k, v in query.items()}

        parsed = parsed._replace(query=urllib.parse.urlencode(query, doseq=True))

        return urllib.parse.urlunsplit(parsed)

    async def cancel(self) -> None:
        await self._delete_export(self._metadata.get_bulk_status_url())

    async def export(self) -> None:
        """
        Bulk export resources from a FHIR server into local ndjson files.

        This call will block for a while, as resources tend to be large, and we may also have to
        wait if the server is busy. Because it is a slow operation, this function will also print
        status updates to the console.

        After this completes, the destination folder will be full of files that look like
        Resource.000.ndjson, like so:

        destination/
          Encounter.000.ndjson
          Encounter.001.ndjson
          Patient.000.ndjson
          log.ndjson

        See http://hl7.org/fhir/uv/bulkdata/export/index.html for details.
        """
        try:
            await self._internal_export()
        except cfs.NetworkError as exc:
            sys.exit(str(exc))

    async def _internal_export(self):
        self._log = BulkExportLogWriter(self._destination)

        rich.get_console().rule()

        if self._metadata.get_bulk_status_url():
            poll_location = self._metadata.get_bulk_status_url()
            self._log.export_id = poll_location
            logging.warning("Resuming bulk FHIR export…")
        else:
            poll_location = await self._kick_off()
            self._metadata.set_bulk_status_url(poll_location)
            logging.warning("Starting bulk FHIR export…")

        # Request status report, until export is done
        response = await self._request_with_delay_status(
            poll_location,
            headers={"Accept": "application/json"},
            log_error=self._log.status_error,
        )
        self._log.status_complete(response)

        # Finished! We're done waiting and can download all the files
        response_json = response.json()

        raw_transaction_time = response_json.get("transactionTime")  # "instant" type
        self.transaction_time = timing.parse_datetime(raw_transaction_time)
        if self.transaction_time is None:
            logging.error(f"Could not parse transactionTime: {raw_transaction_time}")
            self.transaction_time = timing.now()

        # Download all the files
        logging.warning("Bulk FHIR export finished, now downloading resources…")
        await self._download_all_ndjson_files(response_json, "output")
        await self._download_all_ndjson_files(response_json, "error")
        await self._download_all_ndjson_files(response_json, "deleted")

        self._log.export_complete()

        # If we raised an error in the above code, we intentionally will not reach this DELETE
        # call. If we had an issue talking to the server (like http errors), we want to leave the
        # files up there, so the user could try to manually recover.
        await self._delete_export(poll_location)
        self._metadata.set_bulk_status_url(None)

        # Were there any server-side errors during the export?
        error_texts, warning_texts = self._gather_all_messages()
        if warning_texts:
            rich.get_console().print(
                "\n - ".join(["Messages from server:", *sorted(warning_texts)])
            )

        # Make sure we're fully done before we bail because the server told us the export has
        # issues. We still want to DELETE the export in this case. And we still want to download
        # all the files the server DID give us. Servers may have lots of ignorable errors that
        # need human review, before passing back to us as input ndjson.
        if error_texts:
            sys.exit("\n - ".join(["Errors occurred during export:", *sorted(error_texts)]))

    ##############################################################################################
    #
    # Helpers
    #
    ##############################################################################################

    async def _kick_off(self):
        """Initiate bulk export"""
        try:
            response = await self._request_with_delay_status(
                self.export_url,
                headers={"Prefer": "respond-async"},
                target_status_code=202,
            )
        except Exception as exc:
            self._log.kickoff(self.export_url, self._client.capabilities, exc)
            raise

        # Grab the poll location URL for status updates
        poll_location = response.headers["Content-Location"]
        self._log.export_id = poll_location

        self._log.kickoff(self.export_url, self._client.capabilities, response)

        return poll_location

    async def _delete_export(self, poll_url: str) -> bool:
        """
        Send a DELETE to the polling location.

        This could be a mere kindness, so that the server knows it can delete the files.
        But it can also be necessary, as some servers (Epic at least) only let you do one export
        per client/group combo.
        """
        try:
            await self._request_with_delay_status(poll_url, method="DELETE", target_status_code=202)
            return True
        except cfs.NetworkError as err:
            # Don't bail on ETL as a whole, this isn't a show stopper error.
            logging.warning(f"Failed to clean up export job on the server side: {err}")
            return False

    async def _request_with_delay_status(self, *args, **kwargs) -> httpx.Response:
        """
        Requests a file, while respecting any requests to wait longer and telling the user.

        :returns: the HTTP response
        """
        status_box = rich.text.Text()
        with rich.get_console().status(status_box):
            response = await self._request_with_retries(*args, rich_text=status_box, **kwargs)

        if status_box.plain:
            logging.warning(  # pragma: no cover
                f"  Waited for a total of {cli_utils.human_time_offset(self._total_wait_time)}"
            )

        return response

    async def _request_with_retries(
        self,
        path: str,
        *,
        headers: dict | None = None,
        target_status_code: int = 200,
        method: str = "GET",
        log_request: Callable[[], None] | None = None,
        log_error: Callable[[Exception], None] | None = None,
        stream: bool = False,
        rich_text: rich.text.Text | None = None,
    ) -> httpx.Response:
        """
        Requests a file, while respecting any requests to wait longer and telling the user.

        :param path: path to request
        :param headers: headers for request
        :param target_status_code: retries until this status code is returned
        :param method: HTTP method to request
        :param log_request: method to call to report every request attempt
        :param log_error: method to call to report request failures
        :param stream: whether to stream the response
        :returns: the HTTP response
        """

        def _add_new_delay(response: httpx.Response | None, delay: int) -> None:
            # Print a message to the user, so they don't see us do nothing for a while
            if rich_text is not None:
                progress_msg = response and response.headers.get("X-Progress")
                progress_msg = progress_msg or "waiting…"
                formatted_total = cli_utils.human_time_offset(self._total_wait_time)
                formatted_delay = cli_utils.human_time_offset(delay)
                rich_text.plain = (
                    f"{progress_msg} ({formatted_total} so far, waiting for {formatted_delay} more)"
                )

            self._total_wait_time += delay

        def _raise_custom_error(*args) -> typing.NoReturn:
            exc = cfs.NetworkError(*args)
            if log_error:
                log_error(exc)
            raise exc

        # Actually loop, attempting the request multiple times as needed
        while self._total_wait_time < self._TIMEOUT_THRESHOLD:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                stream=stream,
                # These retry times are extremely generous - partly because we can afford to be
                # as a long-running async task and partly because EHR servers seem prone to
                # outages that clear up after a bit.
                retry_delays=[1, 2, 4, 8],  # five tries, 15 minutes total
                request_callback=log_request,
                error_callback=log_error,
                retry_callback=_add_new_delay,
            )

            if response.status_code == target_status_code:
                return response

            # 202 == server is still working on it.
            if response.status_code == 202:
                # Some servers can request unreasonably long delays (e.g. I've seen Cerner
                # ask for five hours), which is... not helpful for our UX and often way
                # too long for small exports. So limit the delay time to 5 minutes.
                delay = min(cfs.parse_retry_after(response, 60), 300)

                _add_new_delay(response, delay)
                await asyncio.sleep(delay)

            else:
                # It feels silly to abort on an unknown *success* code, but the spec has such clear
                # guidance on what the expected response codes are, that it's not clear if a code
                # outside those parameters means we should keep waiting or stop waiting.
                # So let's be strict here for now.
                _raise_custom_error(
                    f"Unexpected status code {response.status_code} "
                    "from the bulk FHIR export server.",
                    response,
                )

        _raise_custom_error("Timed out waiting for the bulk FHIR export to finish.", None)

    def _gather_all_messages(self) -> (set[str], set[str]):
        """
        Parses all error/info ndjson files from the bulk export server.

        :returns: (error messages, non-fatal messages)
        """
        # The spec acknowledges that "error" is perhaps misleading for an array that can contain
        # info messages.
        error_dir = f"{self._destination}/error"

        fatal_messages = set()
        info_messages = set()
        for outcome in cfs.read_multiline_json_from_dir(error_dir, resources.OPERATION_OUTCOME):
            for issue in outcome.get("issue", []):
                text = issue.get("diagnostics")
                text = text or issue.get("details", {}).get("text")
                text = text or issue.get("code")  # code is required at least
                if issue.get("severity") in ("fatal", "error"):
                    fatal_messages.add(text)
                else:
                    info_messages.add(text)

        return fatal_messages, info_messages

    async def _download_all_ndjson_files(self, resource_json: dict, item_type: str) -> None:
        """
        Downloads all exported ndjson files from the bulk export server.

        :param resource_json: the status response from bulk FHIR server
        :param item_type: which type of object to download: output, error, or deleted
        """
        files = resource_json.get(item_type, [])

        # Use the same (sensible) download folder layout as bulk-data-client:
        subfolder = "" if item_type == "output" else item_type

        resource_counts = {}  # how many of each resource we've seen
        coroutines = []
        for file in files:
            count = resource_counts.get(file["type"], 0) + 1
            resource_counts[file["type"]] = count
            filename = f"{file['type']}.{count:03}.ndjson.gz"
            coroutines.append(
                self._download_ndjson_file(
                    file["url"],
                    file["type"],
                    os.path.join(self._destination, subfolder, filename),
                    item_type,
                ),
            )
        await asyncio.gather(*coroutines)

    async def _download_ndjson_file(
        self, url: str, resource_type: str, filename: str, item_type: str
    ) -> None:
        """
        Downloads a single ndjson file from the bulk export server.

        :param url: URL location of file to download
        :param resource_type: the resource type of the file
        :param filename: local path to write data to
        """
        decompressed_size = 0

        response = await self._request_with_retries(
            url,
            headers={"Accept": "application/fhir+ndjson"},
            stream=True,
            log_request=partial(self._log.download_request, url, item_type, resource_type),
            log_error=partial(self._log.download_error, url),
        )
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with gzip.open(filename, "wt", encoding="utf8") as file:
                async for block in response.aiter_text():
                    file.write(block)
                    decompressed_size += len(block)
        except Exception as exc:
            self._log.download_error(url, exc)
            raise cfs.NetworkError(f"Error downloading '{url}': {exc}", None)
        finally:
            await response.aclose()

        lines = ndjson.read_local_line_count(filename)
        self._log.download_complete(url, lines, decompressed_size)

        rel_filename = os.path.relpath(filename, self._destination)
        human_size = cli_utils.human_file_size(response.num_bytes_downloaded)
        logging.warning(f"  Downloaded {rel_filename} ({human_size})")


async def perform_bulk(
    *,
    fhir_url: str,
    bulk_client: cfs.FhirClient,
    filters: filtering.Filters,
    group: str,
    workdir: str,
    managed_dir: str | None = None,
):
    os.makedirs(workdir, exist_ok=True)
    metadata = lifecycle.OutputMetadata(workdir)
    metadata.note_context(filters)

    type_filters = filters.params(bulk=True)
    filters.print_since(bulk=True)

    # See which resources we can skip
    already_done = set()
    if not metadata.get_bulk_status_url():
        for res_type in type_filters:
            if metadata.is_done(res_type):
                logging.warning(f"Skipping {res_type}, already done.")
                already_done.add(res_type)
    res_types = set(type_filters) - already_done

    if res_types:
        exporter = BulkExporter(
            bulk_client,
            res_types,
            export_url(fhir_url, group),
            workdir,
            since=filters.get_bulk_since(),
            type_filter=type_filters,
            metadata=metadata,
        )
        await exporter.export()

        if resources.PATIENT in res_types:
            # Mark down which patients are new to this export, just in case the user later wants to
            # do a crawl of some resources that weren't in this export. The server *should* be
            # giving us all historical resources for new patients, if we exported that resource now.
            # Also, ignore detected deleted patients, because the server should have told us those.
            new, _deleted = merges.find_new_patients(workdir, managed_dir, filters)
            if new:
                metadata.note_new_patients(new)

        for res_type in res_types:
            metadata.mark_done(res_type, exporter.transaction_time)
