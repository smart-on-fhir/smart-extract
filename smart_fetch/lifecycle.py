import datetime
import enum
import json
import os
import re
import sys

import smart_fetch
from smart_fetch import filtering, timing


class MetadataKind(enum.StrEnum):
    MANAGED = enum.auto()
    OUTPUT = enum.auto()

    @classmethod
    def pretty(cls, kind: "MetadataKind") -> str:
        if kind == MetadataKind.MANAGED:
            return f"a {kind} folder"
        else:
            return f"an {kind} folder"


class Metadata:
    def __init__(self, folder: str, kind: MetadataKind):
        self._folder = folder
        self._path = os.path.join(self._folder, ".metadata")
        self._kind = kind
        self._contents = self._read()
        if found_kind := self._contents.get("kind"):
            if found_kind != self._kind:
                sys.exit(
                    f"Folder {self._folder} is not {MetadataKind.pretty(self._kind)},"
                    f" but {MetadataKind.pretty(found_kind)}"
                )

    def _basic_metadata(self) -> dict:
        return {
            "kind": self._kind,
            "timestamp": timing.now().isoformat(),
            "version": smart_fetch.__version__,
        }

    def _read(self) -> dict:
        try:
            with open(self._path, encoding="utf8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _write(self) -> None:
        self._contents.update(self._basic_metadata())
        os.makedirs(self._folder, exist_ok=True)

        tmp_path = f"{self._path}.tmp"
        with open(tmp_path, "w", encoding="utf8") as f:
            json.dump(self._contents, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)


class OutputMetadata(Metadata):
    def __init__(self, folder: str):
        super().__init__(folder, MetadataKind.OUTPUT)

    def note_context(self, filters: filtering.Filters) -> None:
        filter_params = filters.params(with_since=False)
        ordered_filters = {res: sorted(params) for res, params in filter_params.items()}
        if "filters" not in self._contents:
            self._contents["filters"] = ordered_filters
            self._contents["since"] = filters.since
            self._contents["sinceMode"] = filters.since_mode and str(filters.since_mode)
            self._write()
            return

        found_filters = self._contents.get("filters")
        if found_filters != ordered_filters:
            sys.exit(
                f"Folder {self._folder} is for a different set of types and/or filters. "
                f"Expected:\n{self._pretty_filters(ordered_filters)}\n\nbut found:\n"
                f"{self._pretty_filters(found_filters)}"
            )

        found_since = self._contents.get("since")
        if found_since != filters.since:
            sys.exit(
                f"Folder {self._folder} is for a different --since time. "
                f"Expected {filters.since} but found {found_since}."
            )

        found_since_mode = self._contents.get("sinceMode")
        if filters.since and found_since_mode != filters.since_mode:
            sys.exit(
                f"Folder {self._folder} is for a different --since-mode. "
                f"Expected '{filters.since_mode}' but found '{found_since_mode}'."
            )

    def has_same_context(self, *, filters: filtering.Filters) -> bool:
        """
        Determines if this folder is the same exact context (filters and since).

        Note that we don't check what sort of export type this was.
        In theory, the user can swap between bulk and crawl as desired.
        """
        filter_params = filters.params(with_since=False)
        ordered_filters = {res: sorted(params) for res, params in filter_params.items()}
        found_filters = self._contents.get("filters")
        found_since = self._contents.get("since")
        found_since_mode = self._contents.get("sinceMode")

        since_match = found_since == filters.since
        if since_match and filters.since == filtering.SinceMode.AUTO:
            # OK we have to be careful here. We want to allow resuming a previous auto export.
            # But we also want to start a new auto export as needed. So the logic is: "if any
            # of the resources of a previous auto export aren't done, it's a match, and we'll
            # resume - otherwise reject a match, and we'll make a new one"
            found_done = self._contents.get("done", {})
            since_match = any(res_type not in found_done for res_type in filter_params)

        return (
            found_filters == ordered_filters
            and since_match
            and (not filters.since or found_since_mode == filters.since_mode)
        )

    def get_matching_timestamps(self, filters: filtering.Filters) -> dict[str, datetime.datetime]:
        """
        Tells if we have a superset of any resources from `filters`

        Returns the resources that are a match, and that resource's finished timestamp.

        This is used to find previous export timestamps to calculate a new automatic "since" value.
        """
        filter_params = filters.params(with_since=False)
        matches = {}

        # We only match on the same type of "since"
        found_since_mode = self._contents.get("sinceMode")
        if found_since_mode and found_since_mode != filters.since_mode:
            return matches

        for res_type, params_list in self._contents.get("filters", {}).items():
            if res_type not in filter_params:
                continue

            res_params = filter_params[res_type]
            found_params = set(params_list)
            both_empty = not res_params and not found_params
            # We look for a subset here, because multiple type filters is an OR - so if we searched
            # for A OR B before, but now are looking for previous A timestamps, that's a match.
            target_is_subset = res_params and res_params.issubset(found_params)
            if both_empty or target_is_subset:
                # OK this folder is a valid match for our current set of filters.
                # Let's find the timestamps.
                done = self._contents.get("done", {})
                if res_type in done:
                    matches[res_type] = datetime.datetime.fromisoformat(done[res_type])

        return matches

    def note_new_patients(self, patient_ids: set[str]) -> None:
        self._contents["newPatients"] = sorted(patient_ids)
        self._write()

    def get_new_patients(self) -> set[str]:
        return set(self._contents.get("newPatients", []))

    @staticmethod
    def _pretty_filters(filters: dict[str, list[str]]) -> str:
        lines = []
        for res_type, params_list in sorted(filters.items()):
            if params_list:
                lines.extend([f"  {res_type}?{params}" for params in params_list])
            else:
                lines.append(f"  {res_type} (no filter)")
        return "\n".join(lines)

    def is_done(self, tag: str) -> bool:
        return tag in self._contents.get("done", {})

    def mark_done(self, tag: str, timestamp: datetime.datetime | None = None) -> None:
        """
        Marks the given resource or task as done.

        The timestamp is ideally a "transactionTime" style stamp (like in bulk export).
        If not provided, it simply uses now().
        """
        timestamp = timestamp or timing.now()
        done = self._contents.setdefault("done", {})
        done[tag] = timestamp.isoformat()
        self._write()

    def get_earliest_done_date(self) -> datetime.datetime | None:
        if done := self._contents.get("done"):
            return min(datetime.datetime.fromisoformat(time) for time in done.values())
        return None

    def set_bulk_status_url(self, status_url: str | None) -> None:
        if status_url:
            self._contents["bulk-status"] = status_url
        else:
            del self._contents["bulk-status"]
        self._write()

    def get_bulk_status_url(self) -> str | None:
        return self._contents.get("bulk-status")


class ManagedMetadata(Metadata):
    def __init__(self, folder: str):
        super().__init__(folder, MetadataKind.MANAGED)

    def note_context(self, *, fhir_url: str, group: str | None) -> None:
        if "fhir-url" not in self._contents:
            self._contents["fhir-url"] = fhir_url
            self._contents["group"] = group
            self._write()
            return

        # Check FHIR URL, with some extremely basic normalization
        found_url = self._contents["fhir-url"]
        found_url = found_url.removesuffix("/")
        fhir_url = fhir_url.removesuffix("/")
        if found_url != fhir_url:
            sys.exit(
                f"Target folder {os.path.basename(self._path)} is for a different FHIR URL.\n"
                f"Expected {fhir_url}\n"
                f"But found {found_url}"
            )

        # Check group
        found_group = self._contents["group"]
        if found_group != group:
            sys.exit(
                f"Target folder {os.path.basename(self._path)} is for a different Group.\n"
                f"Expected {group}, but found {found_group}."
            )


def list_workdirs(source_dir: str) -> dict[str, tuple[int, str]]:
    """
    Returns workdirs in reverse order (i.e. latest first)

    Return format is filename -> (num, nickname) for filenames like {num}.{nickname}
    """
    try:
        with os.scandir(source_dir) as scanner:
            folders = [entry.name for entry in scanner if entry.is_dir()]
    except FileNotFoundError:
        return {}

    matches = {re.fullmatch(r"(\d+)\.(.*)", folder): folder for folder in folders}
    nums = {int(m.group(1)): (m.group(2), val) for m, val in matches.items() if m}

    return {nums[num][1]: (num, nums[num][0]) for num in sorted(nums, reverse=True)}
