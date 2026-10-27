#!/usr/bin/env python3
"""Capture Linux watcher errors without changing resource limits."""

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess

LIMIT_PATHS = (
    "/proc/self/limits",
    "/proc/self/uid_map",
    "/proc/sys/fs/inotify/max_user_instances",
    "/proc/sys/fs/inotify/max_user_watches",
    "/proc/sys/fs/file-max",
    "/proc/sys/fs/file-nr",
)


def probe_watcher(library, close=os.close):
    initialize = library.inotify_init1
    initialize.argtypes = [ctypes.c_int]
    initialize.restype = ctypes.c_int
    ctypes.set_errno(0)
    # Linux IN_NONBLOCK and IN_CLOEXEC, matching fsnotify.NewWatcher.
    descriptor = initialize(0x800 | 0x80000)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        return {
            "syscall": "inotify_init1",
            "errno": error_number,
            "name": errno.errorcode.get(error_number, "UNKNOWN"),
            "message": os.strerror(error_number),
        }
    close(descriptor)
    return {"syscall": "inotify_init1", "errno": 0}


def capture_probe():
    report = {"uid": os.getuid(), "limits": {}}
    for filename in LIMIT_PATHS:
        try:
            report["limits"][filename] = Path(filename).read_text().strip()
        except OSError as error:
            report["limits"][filename] = str(error)
    report["open_descriptors"] = len(list(Path("/proc/self/fd").iterdir()))
    report["watcher"] = probe_watcher(ctypes.CDLL(None, use_errno=True))
    print(json.dumps(report, indent=2), flush=True)


def diagnostic_pod(pod):
    containers = pod["spec"]["containers"]
    collector = next(
        c for c in containers if c["name"] == "metrics-logger-and-collector"
    )
    training = next(c for c in containers if c["name"] == "training-container")
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "generateName": "katib-watcher-probe-",
            "namespace": pod["metadata"]["namespace"],
            "annotations": {"sidecar.istio.io/inject": "false"},
        },
        "spec": {
            "nodeName": pod["spec"]["nodeName"],
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 45,
            "automountServiceAccountToken": False,
            "securityContext": pod["spec"].get("securityContext", {}),
            "containers": [
                {
                    "name": "watcher-probe",
                    "image": training["image"],
                    "imagePullPolicy": "Never",
                    "command": ["python3", "-c", Path(__file__).read_text(), "--probe"],
                    "securityContext": collector.get("securityContext", {}),
                    "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}},
                }
            ],
        },
    }


def kubectl(*arguments, **options):
    return subprocess.run(
        ["kubectl", "--request-timeout=10s", *arguments],
        text=True,
        capture_output=True,
        timeout=60,
        **options,
    )


def capture_nodes(namespace):
    result = kubectl("get", "pods", "-n", namespace, "-o", "json", check=True)
    nodes = {}
    for pod in json.loads(result.stdout)["items"]:
        names = {c["name"] for c in pod["spec"]["containers"]}
        if {"training-container", "metrics-logger-and-collector"} <= names:
            nodes.setdefault(pod["spec"]["nodeName"], pod)
    for node, pod in nodes.items():
        print(
            json.dumps({"node": node, "source_pod": pod["metadata"]["name"]}),
            flush=True,
        )
        name = None
        try:
            created = kubectl(
                "create",
                "-f",
                "-",
                "-o",
                "json",
                input=json.dumps(diagnostic_pod(pod)),
                check=True,
            )
            name = json.loads(created.stdout)["metadata"]["name"]
            waited = kubectl(
                "wait",
                "-n",
                namespace,
                "pod/" + name,
                "--for=jsonpath={.status.phase}=Succeeded",
                "--timeout=45s",
            )
            print(waited.stdout, waited.stderr, flush=True)
            logs = kubectl("logs", "-n", namespace, name)
            print(logs.stdout, logs.stderr, flush=True)
            status = kubectl("get", "pod", "-n", namespace, name, "-o", "json")
            print(status.stdout, status.stderr, flush=True)
        except (subprocess.SubprocessError, ValueError) as error:
            print(str(error), getattr(error, "stderr", ""), flush=True)
        finally:
            if name:
                deleted = kubectl(
                    "delete", "pod", "-n", namespace, name, "--wait=false"
                )
                print(deleted.stdout, deleted.stderr, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--namespace", default="kubeflow-user-example-com")
    arguments = parser.parse_args()
    if arguments.probe:
        capture_probe()
    else:
        capture_nodes(arguments.namespace)
