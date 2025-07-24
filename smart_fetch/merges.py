"""Code for dealing with merged and newly-appearing Patient resources."""

import os

import cumulus_fhir_support as cfs

from smart_fetch import filtering, lifecycle, resources


def find_new_patients(
    workdir: str,
    managed_dir: str | None,
    filters: filtering.Filters,
) -> tuple[set[str], set[str]]:
    """
    Returns new and deleted patient IDs, in that order.

    This is done by looking back for previous Patient exports and finding whether we have more or
    less patients this time.

    There are a couple sources of new patients (or at least, patients we want to treat as new,
    in the sense of getting their historical resources the first time we see them):
    - Literally just newly appeared patients (user added some to their --mrn-file or the Group got
      expanded)
    - Merged patients (patient A got merged into patient B - maybe both are pre-existing patients,
      but we want to treat patient B as new in the sense that we need to update all their
      historical data in case old patient A resources got updated to point at B)

    And a couple ways of thinking about deleted patients:
    - Maybe the patient got dropped from the Group or --mrn-file
    - Merged patients (patient A got merged into patient B) - which can be a soft delete or a hard
      delete, depending on how the EHR handles it - we follow their lead and only consider the
      patient deleted if they aren't in the export anymore.

    This relies on the fact that patients must all be exported every time (i.e. there's no date
    field for "since" to get just a slice of the patients).
    """
    if not managed_dir:
        return set(), set()

    # Now, we go backward in time to find the latest export for patients (with same filters).
    # Then compare the two sets of patients.
    for folder in lifecycle.list_workdirs(managed_dir):
        folder_path = os.path.join(managed_dir, folder)
        folder_metadata = lifecycle.OutputMetadata(folder_path)
        if resources.PATIENT in folder_metadata.get_matching_timestamps(filters):
            previous_patients = _find_replaced_links(folder_path)
            break
    else:
        # No previous patient export - don't report any new or deleted patients
        return set(), set()

    current_patients = _find_replaced_links(workdir)

    # Check for the easy-to-detect new and deleted patients
    new_patients = set(current_patients) - set(previous_patients)
    deleted_patients = set(previous_patients) - set(current_patients)

    # Also check if we've had any new merges since last export, and mark the replacing patients
    # as new, so we will update their historical records and get any new resources pointed at them.
    # TODO: should we worry about "un-merged" patients? Like A and B split up after being merged.
    for patient, current_replacements in current_patients.items():
        previous_replacements = previous_patients.get(patient, set())
        if current_replacements - previous_replacements:
            new_patients.add(patient)

    return new_patients, deleted_patients


def _find_replaced_links(workdir: str) -> dict[str, set[str]]:
    """
    Return mapping of replacing ID -> replaced IDs (new -> old).
    """
    replaced = {}

    for patient in cfs.read_multiline_json_from_dir(workdir, resources.PATIENT):
        ids = replaced.setdefault(patient["id"], set())
        for link in patient.get("link", []):
            if link.get("type") == "replaces":
                reference = link.get("other", {}).get("reference", "")
                if reference.startswith(f"{resources.PATIENT}/"):
                    ids.add(reference.split("/", 1)[-1])

    return replaced


def find_new_patients_for_resource(
    res_type: str,
    metadata: lifecycle.OutputMetadata,
    managed_dir: str | None,
    filters: filtering.Filters,
) -> set[str]:
    # Check if we have new patients in our current export - if so, easy-peasy, we'll use those.
    if new_patients := metadata.get_new_patients():
        return new_patients

    if not managed_dir:
        return set()  # can't search backwards through exports, so we're done here

    # Now, we go backward in time to find the latest metadata that references new patients, since
    # the last time we exported this resource type. We grab any new patients in any exports along
    # the way (regardless of the filters used - since we can't really know what the user wants,
    # but all new patients are interesting to us).
    for folder in lifecycle.list_workdirs(managed_dir):
        folder_metadata = lifecycle.OutputMetadata(os.path.join(managed_dir, folder))
        if res_type in folder_metadata.get_matching_timestamps(filters):
            break
        new_patients |= folder_metadata.get_new_patients()

    return new_patients
