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
