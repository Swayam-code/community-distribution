import ctypes
import errno
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from katib_watcher_diagnostics import capture_nodes, diagnostic_pod, probe_watcher


class WatcherDiagnosticTest(unittest.TestCase):
    def test_reports_original_system_error(self):
        library = Mock()

        def fail(flags):
            ctypes.set_errno(errno.EMFILE)
            return -1

        library.inotify_init1.side_effect = fail
        close = Mock()
        result = probe_watcher(library, close)
        self.assertEqual(result["errno"], errno.EMFILE)
        self.assertEqual(result["name"], "EMFILE")
        close.assert_not_called()

    def test_preserves_distinct_system_errors(self):
        for error_number in (errno.ENFILE, errno.EPERM, errno.ENOMEM):
            with self.subTest(error_number=error_number):
                library = Mock()

                def fail(flags):
                    ctypes.set_errno(error_number)
                    return -1

                library.inotify_init1.side_effect = fail
                self.assertEqual(probe_watcher(library)["errno"], error_number)

    @patch("katib_watcher_diagnostics.print")
    @patch("katib_watcher_diagnostics.diagnostic_pod", return_value={"kind": "Pod"})
    @patch("katib_watcher_diagnostics.kubectl")
    def test_one_probe_per_node_and_cleanup_after_wait_failure(
        self, command, manifest, output
    ):
        pod = {
            "metadata": {"name": "trial-one"},
            "spec": {
                "nodeName": "worker-one",
                "containers": [
                    {"name": "training-container"},
                    {"name": "metrics-logger-and-collector"},
                ],
            },
        }
        command.side_effect = [
            Mock(stdout=json.dumps({"items": [pod, pod]})),
            Mock(stdout=json.dumps({"metadata": {"name": "watcher-probe"}})),
            subprocess.TimeoutExpired("kubectl wait", 60),
            Mock(stdout="deleted", stderr=""),
        ]
        capture_nodes("custom-profile")
        self.assertEqual(manifest.call_count, 1)
        self.assertEqual(command.call_count, 4)
        self.assertEqual(
            command.call_args.args,
            ("delete", "pod", "-n", "custom-profile", "watcher-probe", "--wait=false"),
        )

    @patch("katib_watcher_diagnostics.kubectl")
    def test_does_not_create_pods_without_trial_containers(self, command):
        command.return_value.stdout = json.dumps({"items": []})
        capture_nodes("custom-profile")
        self.assertEqual(command.call_count, 1)

    def test_closes_successful_watcher(self):
        library = Mock()
        library.inotify_init1.return_value = 17
        close = Mock()
        self.assertEqual(probe_watcher(library, close)["errno"], 0)
        close.assert_called_once_with(17)

    @patch("katib_watcher_diagnostics.Path.read_text", return_value="probe source")
    def test_matches_node_and_collector_security_without_downloading_images(self, read):
        pod = {
            "metadata": {"namespace": "custom-profile"},
            "spec": {
                "nodeName": "worker-two",
                "securityContext": {"runAsUser": 123},
                "containers": [
                    {"name": "training-container", "image": "cached-training:version"},
                    {
                        "name": "metrics-logger-and-collector",
                        "securityContext": {"runAsUser": 456},
                    },
                ],
            },
        }
        result = diagnostic_pod(pod)
        specification = result["spec"]
        container = specification["containers"][0]
        self.assertEqual(result["metadata"]["namespace"], "custom-profile")
        self.assertEqual(specification["nodeName"], "worker-two")
        self.assertEqual(specification["securityContext"], {"runAsUser": 123})
        self.assertEqual(container["securityContext"], {"runAsUser": 456})
        self.assertEqual(container["image"], "cached-training:version")
        self.assertEqual(container["imagePullPolicy"], "Never")
        self.assertFalse(specification["automountServiceAccountToken"])
        self.assertEqual(specification["activeDeadlineSeconds"], 45)


if __name__ == "__main__":
    unittest.main()
