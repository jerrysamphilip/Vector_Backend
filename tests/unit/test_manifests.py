"""
Deployment hardening guard rails (rules P-1, P-2, P-3) for manifests/, the Dockerfile and compose.

P-1  every container non-root, read-only root filesystem, all capabilities dropped, no privilege
     escalation, seccomp RuntimeDefault; namespaces enforce Pod Security `restricted`.
P-2  every image pinned by digest (or the CI placeholder that is replaced by the pushed digest).
P-3  NetworkPolicies (default-deny ingress + allow rules), docs/adr/0008-internal-transport.md.

Pure file parsing (PyYAML comes with uvicorn[standard]); no cluster, network or docker needed.
"""
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "manifests"
DEPLOYMENT_FILES = ["deployment.yaml", "deployment-stage.yaml"]
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
PLACEHOLDER = "<ECR_IMAGE_URI>"


def _docs(name):
    return [d for d in yaml.safe_load_all((MANIFESTS / name).read_text(encoding="utf-8")) if d]


def _pod_spec(doc):
    spec = doc["spec"]
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def _containers():
    for name in DEPLOYMENT_FILES:
        for doc in _docs(name):
            if doc["kind"] not in WORKLOAD_KINDS:
                continue
            pod = _pod_spec(doc)
            for c in pod.get("initContainers", []) + pod["containers"]:
                yield pytest.param(pod, c, id=f"{name}:{doc['metadata']['name']}:{c['name']}")


def _workloads():
    for name in DEPLOYMENT_FILES:
        for doc in _docs(name):
            if doc["kind"] in WORKLOAD_KINDS:
                yield pytest.param(doc, id=f"{name}:{doc['metadata']['name']}")


def test_manifests_define_workloads():
    assert len(list(_containers())) >= 6  # migrate + api + worker, in dev and stage


# ── P-1 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pod,container", _containers())
def test_container_security_context(pod, container):
    psc = pod.get("securityContext") or {}
    sc = container.get("securityContext") or {}
    assert sc.get("runAsNonRoot", psc.get("runAsNonRoot")) is True
    assert sc.get("runAsUser", psc.get("runAsUser", 10001)) != 0
    assert sc.get("readOnlyRootFilesystem") is True
    assert sc.get("allowPrivilegeEscalation") is False
    assert sc.get("privileged", False) is False
    assert "ALL" in (sc.get("capabilities") or {}).get("drop", [])
    assert not (sc.get("capabilities") or {}).get("add")
    seccomp = sc.get("seccompProfile") or psc.get("seccompProfile") or {}
    assert seccomp.get("type") == "RuntimeDefault"


@pytest.mark.parametrize("pod,container", _containers())
def test_writable_paths_are_volumes(pod, container):
    volumes = {v["name"] for v in pod.get("volumes", [])}
    mounts = {m["mountPath"]: m["name"] for m in container.get("volumeMounts", [])}
    assert "/tmp" in mounts, "read-only root: /tmp must be an emptyDir"
    assert set(mounts.values()) <= volumes
    if container.get("command", [""])[-1] != "app.db.migrate":
        assert "/app/uploads" in mounts, "ATTACHMENTS_DIR lives under /app/uploads"


@pytest.mark.parametrize("doc", _workloads())
def test_pod_level_hardening(doc):
    pod = _pod_spec(doc)
    assert pod.get("automountServiceAccountToken") is False
    assert not pod.get("hostNetwork") and not pod.get("hostPID") and not pod.get("hostIPC")
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"


def test_stage_matches_dev_except_namespace():
    dev = (MANIFESTS / "deployment.yaml").read_text(encoding="utf-8")
    stage = (MANIFESTS / "deployment-stage.yaml").read_text(encoding="utf-8")
    assert dev.replace("namespace: sales360\n", "namespace: sales360-stage\n") == stage


def test_namespaces_enforce_restricted_pod_security():
    namespaces = {d["metadata"]["name"]: d["metadata"].get("labels", {})
                  for d in _docs("namespace.yaml") if d["kind"] == "Namespace"}
    used = {d["metadata"]["namespace"] for f in DEPLOYMENT_FILES for d in _docs(f)}
    assert used <= set(namespaces)
    for labels in namespaces.values():
        assert labels.get("pod-security.kubernetes.io/enforce") == "restricted"


def test_dockerfile_runs_as_non_root_without_bytecode_writes():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
    assert users and users[-1] not in {"root", "0"}
    assert "PYTHONDONTWRITEBYTECODE=1" in text


def test_compose_services_are_hardened():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name in ("migrate", "api", "worker", "web"):
        svc = compose["services"][name]
        assert svc.get("read_only") is True, name
        assert "ALL" in svc.get("cap_drop", []), name
        assert "no-new-privileges:true" in svc.get("security_opt", []), name


# ── P-2 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pod,container", _containers())
def test_manifest_images_are_digest_or_ci_placeholder(pod, container):
    image = container["image"]
    assert image == PLACEHOLDER or DIGEST.search(image), image
    assert container.get("imagePullPolicy", "IfNotPresent") != "Never"


def test_dockerfile_from_lines_are_digest_pinned():
    froms = re.findall(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", (ROOT / "Dockerfile").read_text(), re.MULTILINE)
    assert froms and all(DIGEST.search(ref) for ref in froms), froms


def test_pin_images_check_passes():
    spec = importlib.util.spec_from_file_location("pin_images", ROOT / "scripts" / "pin_images.py")
    pin_images = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pin_images)
    assert pin_images.main(["--check"]) == 0
    # --strict refuses the CI placeholder (CI runs it on the rendered manifest)
    assert pin_images.main(["--check", "--strict", str(MANIFESTS / "deployment.yaml")]) == 1


# ── P-3 ─────────────────────────────────────────────────────────────────────


def test_network_policies_present():
    policies = {d["metadata"]["name"]: d["spec"] for d in _docs("networkpolicy.yaml")
                if d["kind"] == "NetworkPolicy"}
    deny = policies["default-deny-ingress"]
    assert deny["podSelector"] == {} and deny["policyTypes"] == ["Ingress"] and not deny.get("ingress")

    api = policies["backend-ingress"]
    assert api["podSelector"]["matchLabels"] == {"app": "backend"}
    assert [p["port"] for rule in api["ingress"] for p in rule["ports"]] == [8001]

    egress = policies["backend-egress"]
    assert set(egress["podSelector"]["matchExpressions"][0]["values"]) == {"backend", "backend-worker"}
    ports = {p["port"] for rule in egress["egress"] for p in rule.get("ports", [])}
    assert {53, 3306, 443, 587, 993} <= ports
    blocks = [peer["ipBlock"] for rule in egress["egress"] for peer in rule.get("to", []) if "ipBlock" in peer]
    assert all("169.254.169.254/32" in b.get("except", []) for b in blocks if b["cidr"] == "0.0.0.0/0")


def test_internal_transport_adr_indexed():
    assert (ROOT / "docs" / "adr" / "0008-internal-transport.md").is_file()
    assert "0008-internal-transport.md" in (ROOT / "docs" / "adr" / "README.md").read_text(encoding="utf-8")
