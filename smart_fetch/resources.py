ALLERGY_INTOLERANCE = "AllergyIntolerance"
BINARY = "Binary"
BUNDLE = "Bundle"
CONDITION = "Condition"
DEVICE = "Device"
DIAGNOSTIC_REPORT = "DiagnosticReport"
DOCUMENT_REFERENCE = "DocumentReference"
ENCOUNTER = "Encounter"
IMMUNIZATION = "Immunization"
MEDICATION = "Medication"
MEDICATION_REQUEST = "MedicationRequest"
OBSERVATION = "Observation"
OPERATION_OUTCOME = "OperationOutcome"
PATIENT = "Patient"
PROCEDURE = "Procedure"
SERVICE_REQUEST = "ServiceRequest"

# All resources that are linked to patients, in the order we usually like to process them.
# Patient first, Encounter, then the rest.
PATIENT_TYPES = [
    PATIENT,
    ENCOUNTER,
    ALLERGY_INTOLERANCE,
    CONDITION,
    DEVICE,
    DIAGNOSTIC_REPORT,
    DOCUMENT_REFERENCE,
    IMMUNIZATION,
    MEDICATION_REQUEST,
    OBSERVATION,
    PROCEDURE,
    SERVICE_REQUEST,
]

SCOPE_TYPES = {
    *PATIENT_TYPES,
    BINARY,
    MEDICATION,
}

# These are the fields to search for to ask "when was this record created?"
# (i.e. the administrative date, not the clinical date for "when did this described event happen?")
# Would be nice if there were a `meta.created` field.
# If you update this, update the get_created_date call below too.
CREATED_SEARCH_FIELDS = {
    ALLERGY_INTOLERANCE: "date",
    CONDITION: "recorded-date",
    # DEVICE has no admin date to search on
    DIAGNOSTIC_REPORT: "issued",
    DOCUMENT_REFERENCE: "date",
    ENCOUNTER: "date",  # clinical date, has no admin date
    IMMUNIZATION: "date",  # clinical date, can't search on `recorded`
    MEDICATION_REQUEST: "authoredon",
    OBSERVATION: "date",  # clinical date, can't search on `issued`
    # PATIENT has no admin date to search on (which is sort of good - merges.py relies on it)
    PROCEDURE: "date",  # clinical date, has no admin date
    SERVICE_REQUEST: "authored",
}


# Get the FHIR version of the search params above.
# Even where there is a better field available in FHIR than searching (e.g. "issued" for
# Observations is available in FHIR, but not when searching), we prefer the searching fields.
# This is so that we can best match apples to apples.
# If you update this, update the CREATED_SEARCH_FIELDS dictionary above too.
def get_created_date(resource: dict) -> str | None:
    res_type = resource["resourceType"]
    if res_type not in CREATED_SEARCH_FIELDS:
        return None

    if res_type == ALLERGY_INTOLERANCE:
        return resource.get("recordedDate")
    elif res_type == CONDITION:
        return resource.get("recordedDate")
    elif res_type == DIAGNOSTIC_REPORT:
        return resource.get("issued")
    elif res_type == DOCUMENT_REFERENCE:
        return resource.get("date")
    elif res_type == ENCOUNTER:
        if parsed := resource.get("period", {}).get("start"):
            return parsed
        if parsed := resource.get("period", {}).get("end"):
            return parsed
    elif res_type == IMMUNIZATION:
        return resource.get("occurrenceDateTime")
    elif res_type == MEDICATION_REQUEST:
        return resource.get("authoredOn")
    elif res_type == OBSERVATION:
        if parsed := resource.get("effectiveDateTime"):
            return parsed
        if parsed := resource.get("effectiveInstant"):
            return parsed
        if parsed := resource.get("effectivePeriod", {}).get("start"):
            return parsed
        if parsed := resource.get("effectivePeriod", {}).get("end"):
            return parsed
    elif res_type == PROCEDURE:
        if parsed := resource.get("performedDateTime"):
            return parsed
        if parsed := resource.get("performedPeriod", {}).get("start"):
            return parsed
        if parsed := resource.get("performedPeriod", {}).get("end"):
            return parsed
    elif res_type == SERVICE_REQUEST:
        return resource.get("authoredOn")

    return None


def get_updated_date(resource: dict) -> str | None:
    return resource.get("meta", {}).get("lastUpdated")
