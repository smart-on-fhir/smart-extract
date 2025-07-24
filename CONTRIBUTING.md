# How to contribute to SMART Fetch

## Did you find a bug?

Excellent!

Please first look at the
[existing GitHub issues](https://github.com/smart-on-fhir/smart-fetch/issues)
to see if it has already been reported.
But if you don't see your bug, please
[file a new GitHub issue](https://github.com/smart-on-fhir/smart-fetch/issues/new),
and we will look at it as soon as we can.

The more detail you can add, the quicker we will be able to fix the bug.

## Do you want to write a patch that fixes a bug?

Even better! Thank you!

### Set up your dev environment

To use the same dev environment as us, you'll want to run these commands:
```sh
pip install .[dev]
pre-commit install
```

This will install the pre-commit hooks for the repo (which automatically enforce styling for you).

### Running unit tests

```sh
pip install .[tests]
pytest
```

### Writing your patch

We roughly follow the Google's
[Python style guide](https://google.github.io/styleguide/pyguide.html),
so please try to follow it too.
It's mostly similar to other style guides,
but some notable aspects are:
- imports should generally be at the module level, not for individual functions
  (i.e. `import asyncio` not `from asyncio import sleep`)
- docstrings with documented arguments have their own format

### How to show us the patch

Open a new GitHub PR and one of the maintainers will notice it and comment.

Please add as much detail as you can about the problem you are solving and ideally even link to
the related existing GitHub issue.

### What to expect

All code changes (even changes made by the main project developers)
go through several automated CI steps and a manual code review.

#### Automatic CI

Here's what GitHub will automatically run:
- unit tests (run `pytest` locally to confirm your tests pass)
- lint tests and static analysis (via `ruff`)

#### Manual review

A project developer will also review your code manually.
Every PR needs one review approval to land.

A reviewer will be looking for things like:
- suitability (not every change makes sense for the project scope or direction)
- maintainability
- general quality

Once approved, you can merge your PR yourself as long as the other GitHub tests pass.
Congratulations, and thank you!

## Design Explanations

### Dates

There are two main kinds of dates internally: "since" dates and transaction times.
These interact a little bit, but are different ideas.

A "since" date is a date given by the user (or calculated for them) that we give to the server,
and the server only returns resources later than that date.

A transaction time is the time up to which an export is "valid" or "complete".
This is a concept taken from bulk exports (but we also apply it to crawls).
The server will return all resources up to that transaction time,
and should not (but may) include resources after that transaction time.

For bulk exports, the server gives us the transaction time.
For crawls, we calculate one from the data.

Transaction times are stored in metadata files,
so that when a future `--since=auto` export happens,
SMART Fetch can go back and use a previous transaction time as the new "since" date.

Bulk exports use a single date for the entire export.
But crawls keep track of a unique transaction time per-resource.
They do this because (a) they can and that gives us more flexibility but also
(b) if one resource is rarely created and the last one was created last month,
we don't want to end up using that one data as the "since" date for all the other resources.
