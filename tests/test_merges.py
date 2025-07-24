"""Tests for merged, new, and deleted patients."""

import httpx

from tests import utils


class MergeTests(utils.TestCase):
    @staticmethod
    def deleted(patient_id: str) -> dict:
        return {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [{"request": {"method": "DELETE", "url": f"Patient/{patient_id}"}}],
        }

    async def test_patient_changes(self):
        # Initial export
        pat1 = {"resourceType": "Patient", "id": "pat1"}
        self.mock_bulk(output=[pat1], params={"_type": "Patient"})
        await self.cli("export", self.folder, "--type=Patient")

        # Second export, confirm we note new patients even with bulk exports
        pat2 = {"resourceType": "Patient", "id": "pat2"}
        pat3 = {"resourceType": "Patient", "id": "pat3"}
        self.mock_bulk(
            output=[pat1, pat2, pat3],
            params={"_type": "Patient", "_since": "2020-10-10T00:00:00Z"},
        )
        await self.cli("export", self.folder, "--type=Patient", "--since=2020-10-10T00:00:00Z")
        self.assert_folder(
            {
                "Patient.001.ndjson.gz": None,
                "Patient.002.ndjson.gz": None,
                "Patient.003.ndjson.gz": None,
                "Patient.004.ndjson.gz": None,
                "001.2021-09-14": None,
                "002.2021-09-14": {
                    ".metadata": {
                        "done": {"Patient": utils.TRANSACTION_TIME},
                        "filters": {"Patient": []},
                        "newPatients": ["pat2", "pat3"],
                        "since": "2020-10-10T00:00:00Z",
                        "sinceMode": "updated",
                        "kind": "output",
                        "timestamp": utils.FROZEN_TIMESTAMP,
                        "version": utils.version,
                    },
                    "log.ndjson": None,
                    "Patient.001.ndjson.gz": None,
                    "Patient.002.ndjson.gz": None,
                    "Patient.003.ndjson.gz": None,
                },
                ".metadata": None,
            }
        )

        # Third export, as a crawl, with new, deleted, merged, and pre-existing patients
        pat2["link"] = [{"type": "replaces", "other": {"reference": "Patient/pat1"}}]
        pat4 = {"resourceType": "Patient", "id": "pat4"}
        self.mock_bulk(
            output=[pat2, pat3, pat4],
            params={"_type": "Patient", "_since": "2021-10-10T00:00:00Z"},
        )
        await self.cli(
            "export",
            self.folder,
            "--type=Patient",
            "--export-mode=crawl",
            "--since=2021-10-10T00:00:00Z",
        )
        self.assert_folder(
            {
                "Patient.001.ndjson.gz": None,
                "Patient.002.ndjson.gz": None,
                "Patient.003.ndjson.gz": None,
                "Patient.004.ndjson.gz": None,
                "Patient.005.ndjson.gz": None,
                "Patient.006.ndjson.gz": None,
                "Patient.007.ndjson.gz": None,
                "001.2021-09-14": None,
                "002.2021-09-14": None,
                "003.2021-09-14": {
                    ".metadata": {
                        "done": {"Patient": utils.TRANSACTION_TIME},
                        "filters": {"Patient": []},
                        "newPatients": ["pat2", "pat4"],
                        "since": "2021-10-10T00:00:00Z",
                        "sinceMode": "updated",
                        "kind": "output",
                        "timestamp": utils.FROZEN_TIMESTAMP,
                        "version": utils.version,
                    },
                    "log.ndjson": None,
                    "Patient.001.ndjson.gz": None,
                    "Patient.002.ndjson.gz": None,
                    "Patient.003.ndjson.gz": None,
                    "deleted": {
                        "Patient.ndjson.gz": [self.deleted("pat1")],
                    },
                },
                ".metadata": None,
            }
        )

    async def test_all_resources_for_new_patients(self):
        """Confirm we get all resources for any new patients, regardless of --since"""
        # Initial export
        pat1 = {"resourceType": "Patient", "id": "pat1"}
        self.mock_bulk(output=[pat1], params={"_type": "Condition,Patient"})
        await self.cli("export", self.folder, "--type=Condition,Patient")

        # Second export of just patients, with a new patient
        pat2 = {"resourceType": "Patient", "id": "pat2"}
        self.mock_bulk(output=[pat1, pat2], params={"_type": "Patient"})
        await self.cli("export", self.folder, "--type=Patient")

        # Crawl export of Condition, should pick up all conditions for new patient
        params = {
            "Condition": [
                httpx.QueryParams(patient="pat1", _lastUpdated=f"gt{utils.TRANSACTION_TIME}"),
                httpx.QueryParams(patient="pat2"),
            ]
        }
        missing = self.set_resource_search_queries(params)
        await self.cli(
            "export",
            self.folder,
            "--type=Condition",
            "--export-mode=crawl",
            f"--since={utils.TRANSACTION_TIME}",
        )
        self.assertEqual(missing, [])

        # Another export of Condition, should treat both patients the same now
        params = {
            "Condition": [
                httpx.QueryParams(patient="pat1", _lastUpdated="gt2024-10-17T12:00:00-05:00"),
                httpx.QueryParams(patient="pat2", _lastUpdated="gt2024-10-17T12:00:00-05:00"),
            ]
        }
        missing = self.set_resource_search_queries(params)
        await self.cli(
            "export",
            self.folder,
            "--type=Condition",
            "--export-mode=crawl",
            "--since=2024-10-17T12:00:00-05:00",
        )
        self.assertEqual(missing, [])

        # An export of Condition & Patient, with a new patient.
        # We should immediately grab all the conditions for new patients.
        pat3 = {"resourceType": "Patient", "id": "pat3"}
        self.mock_bulk(
            output=[pat1, pat2, pat3],
            params={"_type": "Patient", "_since": "2024-10-18T12:00:00-05:00"},
        )
        params = {
            "Condition": [
                httpx.QueryParams(patient="pat1", _lastUpdated="gt2024-10-18T12:00:00-05:00"),
                httpx.QueryParams(patient="pat2", _lastUpdated="gt2024-10-18T12:00:00-05:00"),
                httpx.QueryParams(patient="pat3"),
            ]
        }
        missing = self.set_resource_search_queries(params)
        await self.cli(
            "export",
            self.folder,
            "--type=Condition,Patient",
            "--export-mode=crawl",
            "--since=2024-10-18T12:00:00-05:00",
        )
        self.assertEqual(missing, [])
