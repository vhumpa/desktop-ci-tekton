# Config Overrides

`overrides.json` controls which Jenkins parameters are set for each test run
triggered by the Tekton task (`tasks/desktop-ci-container.yaml`).

The file is fetched at runtime from this repository's GitHub raw URL. If
fetching fails, safe defaults are used (Beaker: `OPENSTACK=false`,
`TESTINGFARM=false`).

## Defaults

Per-architecture base settings. Any architecture **not listed** here falls back
to Beaker (both `OPENSTACK` and `TESTINGFARM` set to `false`).

These set the starting values before overrides are applied.

```json
"defaults": {
  "x86_64":  { "OPENSTACK": "true" },
  "aarch64": { "TESTINGFARM": "true" },
  "s390x":   { "TESTINGFARM": "true" },
  "ppc64le": { "TESTINGFARM": "true" }
}
```

The above means: x86_64 runs on OpenStack by default, secondary architectures
use Testing Farm by default.

## Overrides

A list of rules applied **in order** on top of the defaults. If multiple
overrides match, they all apply and **last match wins** on conflicts.

Each rule can have any combination of these **filter fields** (all specified
filters must match for the rule to apply):

| Filter      | Description                              | Examples                      |
|-------------|------------------------------------------|-------------------------------|
| `component` | Component name extracted from Jenkins URL | `thunderbird`, `gnome-shell`  |
| `arch`      | Architecture                             | `x86_64`, `s390x`, `aarch64`  |
| `rhel`      | RHEL version - exact or range            | `10.2`, `>=10.2`, `<11.0`     |

**Omitting a filter field means "match any"** for that dimension. For example,
an override with only `"rhel": ">=11.0"` will match all components on all
architectures running RHEL 11.0 or newer.

The `params` object contains Jenkins parameters to set when the rule matches.
Any Jenkins job parameter can be used here.

### Common parameters

| Parameter      | Values         | Effect                                   |
|----------------|----------------|------------------------------------------|
| `OPENSTACK`    | `true`/`false` | Run on OpenStack cloud                   |
| `TESTINGFARM`  | `true`/`false` | Run via Testing Farm                     |
| `FIPS`         | `true`/`false` | Enable FIPS mode on the test system      |

When both `OPENSTACK` and `TESTINGFARM` are `false`, the job runs on Beaker.

## Firefox non-FIPS extra run

The Firefox x86_64 RHEL 10.2+ override also defines:

```json
"extra_run": {
  "params": { "OPENSTACK": "false", "TESTINGFARM": "true", "FIPS": "false" },
  "run_tags": "gate-non-fips"
}
```

This starts two builds of the same Jenkins job in parallel: the normal FIPS
gating suite on Beaker and a non-FIPS run on Testing Farm. Both use the same
snapshot image, registry authentication, architecture, and common job settings.
Testing Farm uses its normal pool selection unless `TF_POOL` is supplied.

The extra run is deliberately limited to **Firefox on x86_64 with a final
resolved `FIPS=true` setting** and an `extra_run` configuration. Thunderbird,
secondary architectures, and non-FIPS Firefox runs use the existing single-run
path. No additional version check is made by the task: the existing override
determines which versions run with FIPS.

Matching rules retain the last explicitly specified `extra_run`; a later rule
can set `"extra_run": null` to disable it. A later `FIPS=false` setting also
disables the extra run, even if its configuration remains present.

### Required Firefox mapper change

Before activating this configuration, restore the test on `rhel-10-flatpak`
with **no `gate` tag**:

```yaml
- firefox_negotiates_pqc_tls:
    tags: errata gate-non-fips
```

The primary still selects `RUN_TAGS=gate`, while the extra run selects
`RUN_TAGS=gate-non-fips`. Keeping `gate` on the PQC test would make it run under
FIPS as well. With this arrangement, secondary architectures and other
single-run Firefox scenarios do not execute the PQC test.

### Parameters and results

Extra parameters replace primary values, rather than adding duplicate request
parameters. The task requires `OPENSTACK=false`, `TESTINGFARM=true`,
`FIPS=false`, and `run_tags=gate-non-fips` for the extra run. It rejects changes
to `VERSION`, `AUTH`, or `ARCH`, and clears inherited `RUN_TESTS` for that run.

Each build is tracked through its own Jenkins queue URL. Both must finish with
`SUCCESS` and have valid, nonempty test reports. The extra report must contain
a passed `firefox_negotiates_pqc_tls` case; a missing, failed, or skipped case
fails the task. Report counts are summed into `TEST_OUTPUT`; any reported
failure also fails the task, even if Jenkins reports `SUCCESS`.

- `REQUEST_URL` and `ARTIFACTS_URL` retain the primary-run URLs.
- `EXTRA_REQUEST_URL` and `EXTRA_ARTIFACTS_URL` expose the extra-run URLs, and
  are empty for single-run executions.
- Both resolved build URLs are printed in the task log.

The dual-run wait is bounded to 4 hours 45 minutes, leaving headroom within the
Pipeline's five-hour task timeout. Jenkins request errors, cancelled queues,
invalid responses, and timeout are reported as `ERROR`. As with the existing
single-run path, terminating the task does not automatically stop Jenkins builds.

### Local regression checks

The tests run the actual embedded Bash with mocked Jenkins/config responses
and synthetic credentials. They require Python 3, PyYAML, Bash, and jq:

```bash
python3 -m unittest discover -s tests -v
```

## Examples

**Run thunderbird on Beaker with FIPS on x86_64 RHEL 10.2+:**
```json
{
  "component": "thunderbird",
  "arch": "x86_64",
  "rhel": ">=10.2",
  "params": { "OPENSTACK": "false", "TESTINGFARM": "false", "FIPS": "true" }
}
```

**Run toolbox on Beaker for x86_64 only (any RHEL version):**
```json
{
  "component": "toolbox",
  "arch": "x86_64",
  "params": { "OPENSTACK": "false" }
}
```

**Enable FIPS for all components on RHEL 11.0+:**
```json
{ "rhel": ">=11.0", "params": { "FIPS": "true" } }
```

**Force gnome-shell to Beaker on s390x for RHEL 10.2 specifically:**
```json
{
  "component": "gnome-shell",
  "arch": "s390x",
  "rhel": "10.2",
  "params": { "TESTINGFARM": "false" }
}
```

## Adding new components or RHEL versions

New components and RHEL versions **do not need to be listed** in this file.
They automatically use the architecture defaults. Only add an override when
you need behavior that differs from the default for that architecture.
