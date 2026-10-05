"""Regression tests for the embedded Bash in tasks/desktop-ci-container.yaml.

These tests extract and run the task's real script. Only external services,
Tekton result paths, and the mounted pull-secret path are replaced by local
fixtures; the Jenkins/config decisions and result handling remain production
code.
"""

import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - documented optional test dependency
    yaml = None


TASK_FILE = Path(__file__).parents[1] / "tasks" / "desktop-ci-container.yaml"
JENKINS_URL = "https://jenkins.example.test/job/beaker-firefox-RHEL-10.3"
PULL_SECRET = {
    "auths": {"registry.example.test": {"auth": "c3ludGhldGljLWF1dGg="}}
}


FAKE_CURL = r'''#!/usr/bin/env python3
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

args = sys.argv[1:]
scenario = json.loads(os.environ.get("TEST_SCENARIO", "{}"))
log_path = Path(os.environ["REQUEST_LOG"])
events = json.loads(log_path.read_text()) if log_path.exists() else []

def log(event):
    events.append(event)
    log_path.write_text(json.dumps(events))

def fail():
    sys.exit(22)

url = next((arg for arg in reversed(args) if arg.startswith(("http://", "https://"))), "")
config_url = "https://raw.githubusercontent.com/vhumpa/desktop-ci-tekton/main/config/overrides.json"
if url == config_url:
    log({"kind": "config", "url": url})
    print(json.dumps(scenario.get("config", {"defaults": {}, "overrides": []})))
    sys.exit(0)

if url.endswith("/buildWithParameters") or "/buildWithParameters?" in url:
    if "--data-urlencode" in args:
        transport = "form"
        pairs = []
        for index, arg in enumerate(args[:-1]):
            if arg == "--data-urlencode":
                key, sep, value = args[index + 1].partition("=")
                pairs.append([key, value if sep else ""])
    else:
        transport = "query"
        pairs = [[key, value] for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)]
    params = dict(pairs)
    run = "extra" if params.get("RUN_TAGS") == "gate-non-fips" else "primary"
    log({"kind": "trigger", "run": run, "transport": transport, "url": url, "params": pairs})
    if scenario.get("trigger_error") == run:
        fail()
    queue_id = 102 if run == "extra" else 101
    base = url.split("/job/", 1)[0]
    print("HTTP/1.1 201 Created\r\nLocation: {}/queue/item/{}/\r\n\r".format(base, queue_id))
    sys.exit(0)

queue_match = re.search(r"/queue/item/(\d+)//?api/json(?:\?|$)", url)
if queue_match:
    run = "extra" if queue_match.group(1) == "102" else "primary"
    log({"kind": "queue", "run": run, "url": url})
    if scenario.get("queue_api_error") == run:
        fail()
    if scenario.get("queue_cancelled") == run:
        print(json.dumps({"cancelled": True}))
        sys.exit(0)
    if scenario.get("pending_queues"):
        print(json.dumps({"why": "waiting for an executor"}))
        sys.exit(0)
    if scenario.get("queue_pending_once") == run and sum(
        event["kind"] == "queue" and event["run"] == run for event in events
    ) == 1:
        print(json.dumps({"why": "waiting for an executor"}))
        sys.exit(0)
    base = os.environ["JENKINS_URL"]
    executable = base + ("/102/" if run == "extra" else "/101/")
    if scenario.get("wrong_build_url") == run:
        executable = "https://other.example.test/job/unrelated/1/"
    print(json.dumps({"executable": {"url": executable}}))
    sys.exit(0)

if "/testReport/api/json" in url:
    run = "extra" if "/102/" in url else "primary"
    log({"kind": "report", "run": run, "url": url})
    if scenario.get("report_api_error") == run:
        fail()
    report = scenario.get("reports", {}).get(run)
    if report is None:
        if run == "extra":
            report = {
                "passCount": 3,
                "failCount": 0,
                "skipCount": 0,
                "suites": [{"cases": [{"name": "firefox_negotiates_pqc_tls", "status": "PASSED"}]}],
            }
        else:
            report = {"passCount": 2, "failCount": 0, "skipCount": 1, "suites": []}
    if isinstance(report, str):
        sys.stdout.write(report)
    else:
        print(json.dumps(report))
    sys.exit(0)

build_match = re.search(r"/job/beaker-[^/]+-RHEL-[^/]+/(101|102)//?api/json(?:\?|$)", url)
if build_match:
    run = "extra" if build_match.group(1) == "102" else "primary"
    log({"kind": "build", "run": run, "url": url})
    if scenario.get("build_api_error") == run:
        fail()
    result = scenario.get("statuses", {}).get(run, "SUCCESS")
    if scenario.get("build_running_once") == run and sum(
        event["kind"] == "build" and event["run"] == run for event in events
    ) == 1:
        result = None
    print(json.dumps({"result": result}))
    sys.exit(0)

log({"kind": "unexpected", "url": url, "args": args})
fail()
'''


SLEEP_FUNCTIONS = textwrap.dedent(
    """\
    sleep() {
      local advance="${SLEEP_ADVANCE:-${1:-0}}"
      SECONDS=$((SECONDS + advance))
    }
    """
)


def dual_config():
    """Config yielding an OpenStack FIPS primary and Testing Farm extra run."""
    return {
        "defaults": {},
        "overrides": [
            {
                "component": "firefox",
                "arch": "x86_64",
                "rhel": ">=10",
                "params": {
                    "OPENSTACK": "true",
                    "TESTINGFARM": "false",
                    "FIPS": "true",
                    "CUSTOM_PARAM": "primary-value",
                },
                "extra_run": {
                    "params": {
                        "OPENSTACK": "false",
                        "TESTINGFARM": "true",
                        "FIPS": "false",
                        "EXTRA_ONLY": "extra-value",
                    },
                    "run_tags": "gate-non-fips",
                },
            },
            {
                "component": "firefox",
                "rhel": "<10",
                "params": {"FIPS": "false"},
            },
        ],
    }


class DesktopCiTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if yaml is None:
            raise unittest.SkipTest("PyYAML is required to extract the Tekton task script")
        task = yaml.safe_load(TASK_FILE.read_text())
        cls.original_script = task["spec"]["steps"][0]["script"]

    def run_task(self, *, component="firefox", rhel="10.3", arch="x86_64", config=None,
                 scenario=None, sleep_advance=None):
        tempdir = tempfile.TemporaryDirectory(prefix="desktop-ci-task-test-")
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        results_dir = root / "results"
        results_dir.mkdir()
        result_paths = {
            "REQUEST_URL": results_dir / "request-url",
            "ARTIFACTS_URL": results_dir / "artifacts-url",
            "EXTRA_REQUEST_URL": results_dir / "extra-request-url",
            "EXTRA_ARTIFACTS_URL": results_dir / "extra-artifacts-url",
            "TEST_OUTPUT": results_dir / "test-output",
        }

        script = self.original_script
        script = re.sub(
            r"\$\(results\.([A-Z_]+)\.path\)",
            lambda match: str(result_paths[match.group(1)]),
            script,
        )
        secret_path = root / "pull-secret" / ".dockerconfigjson"
        secret_path.parent.mkdir()
        secret_path.write_text(json.dumps(PULL_SECRET))
        script = script.replace("/etc/secrets/pull-secret-volume/.dockerconfigjson", str(secret_path))
        script_path = root / "task-script.sh"
        script_path.write_text(script)

        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_curl = fake_bin / "curl"
        fake_curl.write_text(FAKE_CURL)
        fake_curl.chmod(0o755)
        bash_env = root / "bash-env"
        bash_env.write_text(SLEEP_FUNCTIONS)
        request_log = root / "requests.json"

        env = os.environ.copy()
        snapshot = {
            "components": [
                {
                    "name": component,
                    "containerImage": "registry.example.test/{}@sha256:deadbeef".format(component),
                }
            ]
        }
        env.update(
            {
                "PATH": str(fake_bin) + os.pathsep + env["PATH"],
                "BASH_ENV": str(bash_env),
                "SNAPSHOT": json.dumps(snapshot),
                "JENKINS_URL": JENKINS_URL.replace("firefox", component).replace("10.3", rhel),
                "JENKINS_USER": "test-user",
                "JENKINS_API_TOKEN": "synthetic-token",
                "ARCH": arch,
                "REQUEST_LOG": str(request_log),
                "TEST_SCENARIO": json.dumps(
                    {"config": config if config is not None else {"defaults": {}, "overrides": []}, **(scenario or {})}
                ),
            }
        )
        if sleep_advance is not None:
            env["SLEEP_ADVANCE"] = str(sleep_advance)

        completed = subprocess.run(
            ["bash", str(script_path)],
            cwd=root,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=20,
        )
        events = json.loads(request_log.read_text()) if request_log.exists() else []
        outputs = {
            name: path.read_text().strip() if path.exists() else None
            for name, path in result_paths.items()
        }
        return completed, events, outputs

    @staticmethod
    def event_list(events, kind):
        return [event for event in events if event["kind"] == kind]

    @staticmethod
    def params_for(trigger):
        return {key: value for key, value in trigger["params"]}

    @staticmethod
    def assert_unique_param_keys(test, trigger):
        keys = [key for key, _ in trigger["params"]]
        test.assertEqual(len(keys), len(set(keys)), "Jenkins trigger contains duplicate parameters")

    def assert_dual_success(self, completed, events, outputs):
        self.assertEqual(completed.returncode, 0, completed.stdout)
        triggers = self.event_list(events, "trigger")
        self.assertEqual([trigger["run"] for trigger in triggers], ["primary", "extra"])
        primary, extra = triggers
        self.assertEqual(primary["transport"], "form")
        self.assertEqual(extra["transport"], "form")
        self.assertEqual(primary["url"], JENKINS_URL + "/buildWithParameters")
        self.assertEqual(extra["url"], JENKINS_URL + "/buildWithParameters")
        self.assert_unique_param_keys(self, primary)
        self.assert_unique_param_keys(self, extra)
        primary_params = self.params_for(primary)
        extra_params = self.params_for(extra)
        for key in ("VERSION", "AUTH", "ARCH"):
            self.assertEqual(primary_params[key], extra_params[key])
        self.assertEqual(primary_params["VERSION"], "SNAPSHOT:registry.example.test/firefox@sha256:deadbeef")
        self.assertEqual(primary_params["AUTH"], "registry.example.test:c3ludGhldGljLWF1dGg=")
        self.assertEqual(primary_params["ARCH"], "x86_64")
        self.assertEqual(primary_params["RUN_TAGS"], "gate")
        self.assertEqual(extra_params["RUN_TAGS"], "gate-non-fips")
        self.assertEqual(primary_params["OPENSTACK"], "true")
        self.assertEqual(primary_params["TESTINGFARM"], "false")
        self.assertEqual(primary_params["FIPS"], "true")
        self.assertEqual(extra_params["OPENSTACK"], "false")
        self.assertEqual(extra_params["TESTINGFARM"], "true")
        self.assertEqual(extra_params["FIPS"], "false")
        self.assertEqual(primary_params["CUSTOM_PARAM"], "primary-value")
        self.assertEqual(extra_params["CUSTOM_PARAM"], "primary-value")
        self.assertEqual(extra_params["EXTRA_ONLY"], "extra-value")

        queues = self.event_list(events, "queue")
        self.assertTrue(queues)
        self.assertLess(
            max(events.index(trigger) for trigger in triggers),
            events.index(queues[0]),
            "both Jenkins runs must be triggered before polling either queue",
        )
        self.assertEqual(outputs["REQUEST_URL"], "https://jenkins.example.test/queue/item/101/")
        self.assertEqual(outputs["EXTRA_REQUEST_URL"], "https://jenkins.example.test/queue/item/102/")
        self.assertEqual(outputs["ARTIFACTS_URL"], JENKINS_URL + "/101/artifact/artifacts/")
        self.assertEqual(outputs["EXTRA_ARTIFACTS_URL"], JENKINS_URL + "/102/artifact/artifacts/")

    def test_firefox_fips_run_triggers_both_and_merges_reports(self):
        completed, events, outputs = self.run_task(config=dual_config())

        self.assert_dual_success(completed, events, outputs)
        result = json.loads(outputs["TEST_OUTPUT"])
        self.assertEqual(
            {key: result[key] for key in ("result", "successes", "failures", "warnings")},
            {"result": "SUCCESS", "successes": 5, "failures": 0, "warnings": 1},
        )
        self.assertTrue(result["timestamp"].isdigit())
        self.assertEqual(len(self.event_list(events, "report")), 2)

    def test_checked_in_overrides_limit_extra_run_to_fips_firefox(self):
        config = json.loads((TASK_FILE.parents[1] / "config" / "overrides.json").read_text())
        cases = (
            ("firefox", "10.3", "x86_64", 2),
            ("firefox", "10.1", "x86_64", 1),
            ("firefox", "10.3", "aarch64", 1),
            ("firefox", "10.3", "s390x", 1),
            ("firefox", "10.3", "ppc64le", 1),
            ("thunderbird", "10.3", "x86_64", 1),
            ("gnome-shell", "10.3", "x86_64", 1),
        )
        for component, rhel, arch, expected in cases:
            with self.subTest(component=component, rhel=rhel, arch=arch):
                completed, events, outputs = self.run_task(
                    component=component, rhel=rhel, arch=arch, config=config,
                )
                self.assertEqual(completed.returncode, 0, completed.stdout)
                triggers = self.event_list(events, "trigger")
                self.assertEqual(len(triggers), expected)
                primary = self.params_for(triggers[0])
                if expected == 2:
                    self.assertEqual(primary["FIPS"], "true")
                    self.assertEqual(primary["OPENSTACK"], "false")
                    self.assertEqual(primary["TESTINGFARM"], "false")
                    extra = self.params_for(triggers[1])
                    self.assertEqual(extra["TESTINGFARM"], "false")
                    self.assertEqual(extra["OPENSTACK"], "true")
                    self.assertEqual(extra["FIPS"], "false")
                    self.assertEqual(extra["RUN_TAGS"], "gate-non-fips")
                    for key in ("VERSION", "AUTH", "ARCH", "QECORE_COREDUMP_FETCH"):
                        self.assertEqual(primary[key], extra[key])
                else:
                    self.assertEqual(triggers[0]["transport"], "query")
                    self.assertEqual(outputs["EXTRA_REQUEST_URL"], "")
                    self.assertEqual(outputs["EXTRA_ARTIFACTS_URL"], "")

    def test_queues_and_builds_can_wait_independently(self):
        completed, events, outputs = self.run_task(
            config=dual_config(),
            scenario={"queue_pending_once": "primary", "build_running_once": "extra"},
        )
        self.assert_dual_success(completed, events, outputs)
        self.assertEqual(len([event for event in events if event["kind"] == "queue"
                              and event["run"] == "primary"]), 2)
        self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "SUCCESS")

    def test_extra_run_can_use_openstack_instead_of_testing_farm(self):
        config = dual_config()
        config["overrides"][0]["extra_run"]["params"].update(
            {"OPENSTACK": "true", "TESTINGFARM": "false"}
        )
        completed, events, outputs = self.run_task(config=config)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        triggers = self.event_list(events, "trigger")
        self.assertEqual(len(triggers), 2)
        primary, extra = map(self.params_for, triggers)
        self.assertEqual(extra["OPENSTACK"], "true")
        self.assertEqual(extra["TESTINGFARM"], "false")
        self.assertEqual(extra["FIPS"], "false")
        self.assertEqual(extra["RUN_TAGS"], "gate-non-fips")
        for key in ("VERSION", "AUTH", "ARCH"):
            self.assertEqual(primary[key], extra[key])
        self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "SUCCESS")

    def test_explicit_null_extra_run_retains_single_path(self):
        config = dual_config()
        config["overrides"].append({"component": "firefox", "params": {}, "extra_run": None})
        completed, events, outputs = self.run_task(config=config)
        self.assertEqual(completed.returncode, 0, completed.stdout)
        triggers = self.event_list(events, "trigger")
        self.assertEqual(len(triggers), 1)
        self.assertEqual(triggers[0]["transport"], "query")
        self.assertEqual(self.params_for(triggers[0])["FIPS"], "true")
        self.assertEqual(outputs["EXTRA_REQUEST_URL"], "")

    def test_invalid_extra_settings_fail_before_triggering(self):
        for key, value in (("TESTINGFARM", "false"), ("OPENSTACK", "true"),
                           ("FIPS", "true"), ("VERSION", "wrong-snapshot"),
                           ("AUTH", "wrong-auth"), ("ARCH", "aarch64")):
            with self.subTest(key=key):
                config = dual_config()
                config["overrides"][0]["extra_run"]["params"][key] = value
                completed, events, outputs = self.run_task(config=config)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertEqual(self.event_list(events, "trigger"), [])
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")

    def test_legacy_single_run_is_unchanged_for_other_components_and_non_fips_cases(self):
        cases = (
            ("thunderbird", "10.3", "x86_64", None),
            ("gnome-shell", "10.3", "x86_64", None),
            ("firefox", "10.3", "aarch64", None),
            ("firefox", "9.6", "x86_64", dual_config()),
        )
        for component, rhel, arch, config in cases:
            with self.subTest(component=component, rhel=rhel, arch=arch):
                completed, events, outputs = self.run_task(
                    component=component, rhel=rhel, arch=arch,
                    config=config if config is not None else dual_config(),
                )
                self.assertEqual(completed.returncode, 0, completed.stdout)
                triggers = self.event_list(events, "trigger")
                self.assertEqual(len(triggers), 1)
                self.assertEqual(triggers[0]["run"], "primary")
                self.assertEqual(triggers[0]["transport"], "query")
                self.assertIn("buildWithParameters?", triggers[0]["url"])
                params = self.params_for(triggers[0])
                self.assertEqual(params["VERSION"], "SNAPSHOT:registry.example.test/{}@sha256:deadbeef".format(component))
                self.assertEqual(params["AUTH"], "registry.example.test:c3ludGhldGljLWF1dGg=")
                self.assertEqual(params["ARCH"], arch)
                self.assertEqual(params["RUN_TAGS"], "gate")
                self.assert_unique_param_keys(self, triggers[0])
                self.assertEqual(outputs["EXTRA_REQUEST_URL"], "")
                self.assertEqual(outputs["EXTRA_ARTIFACTS_URL"], "")
                self.assertEqual(outputs["REQUEST_URL"], "https://jenkins.example.test/queue/item/101/")
                self.assertEqual(outputs["ARTIFACTS_URL"], triggers[0]["url"].split("/buildWithParameters", 1)[0] + "/101/artifact/artifacts/")
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "SUCCESS")
                if component == "firefox" and rhel == "9.6":
                    self.assertEqual(self.params_for(triggers[0])["FIPS"], "false")

    def test_last_matching_fips_false_override_disables_extra_run(self):
        config = dual_config()
        config["overrides"].append(
            {
                "component": "firefox",
                "arch": "x86_64",
                "rhel": ">=10",
                "params": {"FIPS": "false"},
            }
        )

        completed, events, outputs = self.run_task(config=config)

        self.assertEqual(completed.returncode, 0, completed.stdout)
        triggers = self.event_list(events, "trigger")
        self.assertEqual(len(triggers), 1)
        self.assertIn("buildWithParameters?", triggers[0]["url"])
        params = self.params_for(triggers[0])
        self.assertEqual(params["FIPS"], "false")
        self.assertEqual(outputs["EXTRA_REQUEST_URL"], "")

    def test_successful_jenkins_status_with_fail_count_is_failure(self):
        completed, events, outputs = self.run_task(
            config=dual_config(),
            scenario={"reports": {"primary": {"passCount": 4, "failCount": 1, "skipCount": 0, "suites": []}}},
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        result = json.loads(outputs["TEST_OUTPUT"])
        self.assertEqual(result["result"], "FAILURE")
        self.assertEqual((result["successes"], result["failures"], result["warnings"]), (7, 1, 0))

    def test_primary_and_extra_jenkins_failure_statuses_map_to_tekton_results(self):
        for run in ("primary", "extra"):
            for jenkins_status, expected in (("FAILURE", "FAILURE"), ("UNSTABLE", "FAILURE"), ("ABORTED", "ERROR")):
                with self.subTest(run=run, status=jenkins_status):
                    completed, events, outputs = self.run_task(
                        config=dual_config(), scenario={"statuses": {run: jenkins_status}}
                    )
                    self.assertNotEqual(completed.returncode, 0, completed.stdout)
                    result = json.loads(outputs["TEST_OUTPUT"])
                    self.assertEqual(result["result"], expected)
                    self.assertEqual((result["successes"], result["failures"], result["warnings"]), (5, 0, 1))
                    self.assertEqual(len(self.event_list(events, "report")), 2)

    def test_extra_run_requires_a_passing_pqc_case(self):
        reports = (
            {"passCount": 3, "failCount": 0, "skipCount": 0, "suites": []},
            {
                "passCount": 2, "failCount": 0, "skipCount": 1,
                "suites": [{"cases": [{"name": "firefox_negotiates_pqc_tls", "status": "SKIPPED"}]}],
            },
            {
                "passCount": 2, "failCount": 1, "skipCount": 0,
                "suites": [{"cases": [{"name": "firefox_negotiates_pqc_tls", "status": "FAILED"}]}],
            },
        )
        for report in reports:
            with self.subTest(report=report):
                completed, _, outputs = self.run_task(
                    config=dual_config(), scenario={"reports": {"extra": report}}
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                result = json.loads(outputs["TEST_OUTPUT"])
                self.assertEqual(result["result"], "ERROR")
                self.assertIn("PQC test is missing, failed, or skipped", completed.stdout)

    def test_empty_or_invalid_test_reports_are_rejected(self):
        reports = (
            ("primary", {"passCount": 0, "failCount": 0, "skipCount": 0, "suites": []}),
            ("primary", "not-json"),
            ("extra", {"passCount": 0, "failCount": 0, "skipCount": 0, "suites": []}),
            ("extra", "not-json"),
        )
        for run, report in reports:
            with self.subTest(run=run, report=report):
                completed, _, outputs = self.run_task(
                    config=dual_config(), scenario={"reports": {run: report}}
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
                self.assertIn("test report is invalid or empty", completed.stdout)

    def test_canceled_primary_or_extra_queue_is_rejected(self):
        for run in ("primary", "extra"):
            with self.subTest(run=run):
                completed, events, outputs = self.run_task(
                    config=dual_config(), scenario={"queue_cancelled": run}
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
                self.assertIn("queue was cancelled", completed.stdout)
                self.assertEqual(len(self.event_list(events, "trigger")), 2)

    def test_jenkins_api_errors_are_reported(self):
        for scenario, message in (
            ({"queue_api_error": "extra"}, "extra queue request failed"),
            ({"build_api_error": "primary"}, "primary build request failed"),
            ({"report_api_error": "extra"}, "extra test report request failed"),
        ):
            with self.subTest(scenario=scenario):
                completed, _, outputs = self.run_task(config=dual_config(), scenario=scenario)
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
                self.assertIn(message, completed.stdout)

    def test_mismatched_executable_url_is_rejected(self):
        completed, _, outputs = self.run_task(
            config=dual_config(), scenario={"wrong_build_url": "extra"}
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
        self.assertIn("extra queue returned an unexpected build URL", completed.stdout)

    def test_dual_run_wait_timeout_fails_without_real_waiting(self):
        completed, events, outputs = self.run_task(
            config=dual_config(), scenario={"pending_queues": True}, sleep_advance=17100
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
        self.assertIn("Timed out waiting for Jenkins builds", completed.stdout)
        self.assertEqual(len(self.event_list(events, "trigger")), 2)
        self.assertEqual(len(self.event_list(events, "queue")), 2)

    def test_trigger_error_stops_before_polling(self):
        for run in ("primary", "extra"):
            with self.subTest(run=run):
                completed, events, outputs = self.run_task(
                    config=dual_config(), scenario={"trigger_error": run}
                )
                self.assertNotEqual(completed.returncode, 0, completed.stdout)
                self.assertEqual(json.loads(outputs["TEST_OUTPUT"])["result"], "ERROR")
                self.assertIn("trigger failed", completed.stdout)
                self.assertEqual(self.event_list(events, "queue"), [])


if __name__ == "__main__":
    unittest.main()
