---
title: Exporting
parent: SMART Fetch
nav_order: 1
# audience: non-programmers vaguely familiar with the project
# type: how-to
---

# Exporting Data in Bulk

By default, SMART Fetch will perform a [bulk export](https://hl7.org/fhir/uv/bulkdata/export.html)
for a wide variety of FHIR resource types.

```shell
smart-fetch export \
  --fhir-url https://bulk-data.smarthealthit.org/fhir \
  /tmp/export
```

## Defining the Cohort

Working with your EHR, you should first define a FHIR Group.
This process will be vendor-specific.

(But as an example for Epic, you have to first define a Registry,
then have your Epic contacts turn that into a FHIR Group.)

Once you have a Group identifier, simply pass it in with `--group ID`.

## Filtering

You can limit which resource types are exported by passing `--type Patient,Condition`.

You can also filter further by adding
[type filters](https://hl7.org/fhir/uv/bulkdata/export.html#_typefilter-experimental-query-parameter)
like `--type-filter=Patient?active=true`.

Adding multiple `--type-filter` arguments will act as a logical OR and find resources that match
any of your type filters.

### Default Observation Filtering

Because Observation typically has the largest volume of resources,
SMART Fetch saves time and space by only exporting Observations with one of the
[nine standard categories](https://www.hl7.org/fhir/R4/valueset-observation-category.html).

## Crawling

"Crawling" is the process of faking a bulk export by going through every patient in your cohort,
and searching for all their resources one-by-one using traditional
[FHIR search](https://www.hl7.org/fhir/R4/search.html).

This is useful if you don't have permission to start a bulk export or your EHR vendor does not
support bulk exports (or is not very performant when running a bulk export).
But in general, if you don't need to use this approach, stick to actual bulk exports.
They should be faster.

It's easy to start a crawl instead of a bulk export by simply passing `--export-mode=crawl`.

{: .note }
Crawling is the default export mode for Epic servers,
because Epic's current bulk export implementation has limitations
(in terms of both recommended cohort size and performance).
You can override this with `--export-mode=bulk`.

### Custom Cohorts

By default, a crawl will use an initial bulk export to get the list of Patients
from a defined FHIR Group just like the normal bulk export flow.
After that, it will use those exported Patients to do non-bulk searches.

But if you don't even have a FHIR Group at all,
you can also specify a custom cohort with a file of MRN values. 

Pass in _both_:
- `--mrn-file` which can be either a text file with one MRN per line
  or a CSV file with a column labelled `MRN` (case-insensitive)
- `--mrn-system` which is a code system that your institution/vendor uses to identify
  the patient MRN identifiers

#### Example
You may have a Patient resource that looks like this:
```json
{
  "resourceType": "Patient",
  "identifier": [
    {
      "system": "uri:oid:1.2.3.4",
      "value": "abc"
    }
  ]
}
```

You can pass in `--mrn-system=uri:oid:1.2.3.4`
and `--mrn-file=mrns.txt` (where that file contains a line of `abc`)
to have SMART Fetch use a cohort that includes this patient.

### New and Deleted Patients

When crawling, SMART Fetch will notice new and deleted patients, just like a bulk export would.

Meaning, historical resources for new and merged patients may be included in the crawl,
even outside the `--since` timespan.
New patients added to the Group or the MRN file will be noticed,
as well as patient records that absorb another record when two patient records merge.
In that case, we'll pull the historical resources for the surviving record,
to make sure we have all the updated resources.

And if a patient is deleted by the EHR (or removed from the Group or MRN file),
that fact will be recorded in a `deleted/` folder, just like it would be for a bulk export.
Downstream tooling might use that information.

This all relies on looking back at previous exports in the export folder.
Noticing new and deleted patients should automatically on the server side for a bulk export,
but SMART Fetch has to do them manually for a crawl.

## Resuming

If your bulk export (or crawl) gets interrupted, just run the SMART Fetch command again with the
same arguments, and it will be resumed.

## New Exports

Every new export you do with different `--type`, `--type-filter`, or `--since` parameters will
go into its own subdirectory with its own log.
Then a symbolic link (symlink) will be created in the target folder pointing to the newly
downloaded files.

For example, after a couple exports (exporting Conditions and Observations separately),
you might have a folder that looks like:
```
001.2025-06-26/
    Condition.001.ndjson.gz
    log.ndjson
002.2025-06-27/
    Observation.001.ndjson.gz
    log.ndjson
Condition.001.ndjson.gz -> 001.2025-06-26/Condition.001.ndjson.gz
Observation.001.ndjson.gz -> 002.2025-06-27/Observation.001.ndjson.gz
```

This lets you work with each individual export or the entire group of exports,
depending on your use case at the time.

If you want more predictably named subfolders, pass `--nickname my-nickname` and the folder will
be named something like `003.my-nickname` instead of using the current date.
