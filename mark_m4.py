#!/usr/bin/env python3
"""
INF 345 Milestone 4 - your service on Kubernetes.

Due Sunday 15 November 2026, 23:59 Almaty time (UTC+5) = 18:59 UTC. Deadlines are
judged by commit time.

    python3 mark_m4.py --path .                  # the static checks: no cluster needed
    python3 mark_m4.py --path . --kind           # + the live checks, in a throwaway kind cluster
    python3 mark_m4.py --path . --kind-cluster ci   # + the live checks, in a kind cluster you have
    python3 mark_m4.py --repo https://github.com/<you>/<project> --kind --at-deadline

Where the evidence comes from:
  - your repository: the manifests in k8s/ (the files directly inside it, like
    `kubectl apply -f k8s/`), and the whole git history (for credentials)
  - a kind cluster: the marker applies YOUR k8s/ to it and watches what happens.
    The official mark runs in kind on GitHub's machines (Intel, Kubernetes 1.37.0).

THE SECRET CONVENTION. A Secret never goes into Git - not even base64. Your Deployment
reads at least one value from a Secret named `app-secret`, which is NOT in k8s/. You
create it yourself on your cluster:
    kubectl create secret generic app-secret --from-literal=API_KEY="$(openssl rand -hex 16)"
The marker creates `app-secret` itself, with a new random value for every key your
Deployment references, before it applies k8s/.

Five gates. Any gate failure scores the milestone 0.
  manifests      k8s/ exists and parses; only allowed kinds; one Deployment; a Service;
                 every container runs your own image (or the course's rescue image)
  safe to run    nothing that could take over the machine: no hostPath, no privileged,
                 no hostNetwork/hostPID/hostIPC, no added capabilities, no hostPort, ...
  no secrets     no kind: Secret in k8s/, and no secret written in plain text in any file
  no credentials no credential anywhere in the git history (the same rule as M1-M3)
  dry run        `kubectl apply --dry-run=server` accepts every file   (live only)

Then 100 points in four blocks of 25. A block scores 25 only if every check in it
passes, static and live. The report lists every problem at once.
  it deploys     your image by its commit SHA (never latest), 2+ replicas, runAsNonRoot;
                 LIVE: it rolls out - every Pod ready within 180 s
  it serves      a Service selects your Pods and targets your PORT;
                 LIVE: all Pods are ready endpoints, and /healthz answers through the Service
  the probes     liveness and readiness: httpGet /healthz on your PORT, sane timings;
  are real       LIVE: the marker FREEZES one Pod's processes. Readiness must take it out of
                 the Service's endpoints (while the other Pods keep answering), liveness must
                 restart it, and it must come back ready
  it is          PORT and another setting from a ConfigMap in k8s/, a value from app-secret,
  configured,    CPU and memory requests and limits;
  not hardcoded  LIVE: the marker deploys a second copy with a random PORT that it writes
                 into YOUR ConfigMap. It must roll out and answer on that port

A block the marker could not check (no cluster, the cluster would not start) says
NOT CHECKED. That is not a zero: it means "run it again". Without a cluster, the static
checks still tell you everything that can be read from your files.

Exit code: with a cluster, 0 only if the score is 100; without one, 0 if no static check
failed. Nothing in this file is hidden from students: it is the rubric.
"""

import argparse
import copy
import datetime
import glob
import json
import os
import random
import re
import secrets as pysecrets
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import yaml
except ImportError:
    print("::error::PyYAML is required: sudo apt install python3-yaml  (or pip install pyyaml)")
    sys.exit(2)


DEADLINE = "2026-11-15T23:59:59+05:00"          # Sunday 15 Nov, 23:59 Almaty (UTC+5)
DEADLINE_UTC = (datetime.datetime.fromisoformat(DEADLINE)
                .astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
KIND_NODE_IMAGE = ("kindest/node:v1.37.0@sha256:"
                   "a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5")
KIND_CLUSTER = "inf345-m4"
SECRET_NAME = "app-secret"
DEFAULT_SECRET_KEY = "API_KEY"      # created when app-secret is used without naming keys
NS_MAIN, NS_CONFIG = "m4", "m4-config"
# The course's rescue image: a working notes service anyone may deploy if their own M3
# image is broken. It costs the 'it deploys' block and nothing else.
RESCUE_IMAGES = ("ghcr.io/inf345/notes-rescue",)
MIN_REPLICAS = 2
ROLLOUT_SECONDS = 180
READINESS_BUDGET = 60     # readiness periodSeconds x failureThreshold, at most
LIVENESS_BUDGET = 120     # liveness  periodSeconds x failureThreshold, at most
STARTUP_BUDGET = 180      # startup   periodSeconds x failureThreshold, at most
INITIAL_DELAY_MAX = 60    # use a startupProbe for anything slower
TIMEOUT_MAX = 10          # probe timeoutSeconds
GRACE_MAX = 60            # terminationGracePeriodSeconds
HEALTHZ_TRIES = 5
POLL_SECONDS = 2

ALLOWED_KINDS = {"Deployment", "Service", "ConfigMap", "ServiceAccount",
                 "PodDisruptionBudget", "HorizontalPodAutoscaler", "NetworkPolicy"}
# Read but not applied: the marker's cluster has no Gateway controller, and it chooses
# its own namespaces. Everything else is refused before anything is applied.
IGNORED_KINDS = {"Namespace", "Gateway", "GatewayClass", "HTTPRoute", "GRPCRoute",
                 "Ingress", "IngressClass", "Kustomization"}
SAFE_VOLUME_TYPES = {"configMap", "secret", "emptyDir", "projected", "downwardAPI"}
PROBES = ("livenessProbe", "readinessProbe", "startupProbe")


# ================================================================ helpers

def run(cmd, cwd=None, timeout=120, env=None, input=None):
    """Run a command, never raise. Returns (rc, stdout, stderr)."""
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.run(cmd, cwd=cwd, env=e, timeout=timeout, capture_output=True,
                           encoding="utf-8", errors="replace", input=input)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except OSError as exc:
        return 127, "", str(exc)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def repo_slug(url):
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def dig(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def as_int(v, default=None):
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*\d+\s*", v):
        return int(v)
    return default


def short(text, n=240):
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= n else text[:n - 3] + "..."


class Clock:
    """Time, replaceable in the tests."""
    def now(self):
        return time.monotonic()

    def sleep(self, s):
        time.sleep(s)


# ======================================================= credentials (= M1-M3)

HIGH_CONFIDENCE = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"gh[pousr]_[A-Za-z0-9]{36,}", "GitHub token"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub fine-grained token"),
    (r"-----BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----", "private key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
]
DOC_VALUES = re.compile(r"(?i)(EXAMPLE|wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY)")
FUZZY = [
    (r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*"
     r"['\"][^'\"\s]{12,}['\"]", "hardcoded credential"),
]
PLACEHOLDER = re.compile(
    r"(?i)(example|sample|placeholder|dummy|change[_-]?me|replace[_-]?me"
    r"|your[_\-\w]*|[_\-]here\b|xxx+|<[^>]+>|\$\{[^}]+\}|\$\([^)]+\)|\bfake\b"
    r"|redacted|todo|test[_-]?token|secret[_-]?key[_-]?here)")
SECRETISH_NAME = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key"
                            r"|credential|access[_-]?key)")


def scan_line(line):
    for pat, label in HIGH_CONFIDENCE:
        m = re.search(pat, line)
        if m and not DOC_VALUES.search(m.group(0)):
            return f"{label}: {m.group(0)[:40]}"
    for pat, label in FUZZY:
        m = re.search(pat, line)
        if m and not PLACEHOLDER.search(m.group(0)):
            return f"{label}: {m.group(0)[:60]}"
    return None


# ============================================================== manifests

def load_manifests(root):
    """Read the files directly in <root>/k8s/ as `kubectl apply -f k8s/` would.

    Returns (docs, problems, raw_texts). Each doc gets '_file'."""
    kdir = os.path.join(root, "k8s")
    if not os.path.isdir(kdir):
        return None, ["there is no k8s/ folder at the top of your repository"], {}
    files = sorted(f for f in glob.glob(os.path.join(kdir, "*"))
                   if os.path.isfile(f) and f.endswith((".yaml", ".yml", ".json")))
    nested = sorted(os.path.relpath(f, root) for f in glob.glob(os.path.join(kdir, "*", "**"),
                                                                recursive=True)
                    if os.path.isfile(f) and f.endswith((".yaml", ".yml", ".json")))
    docs, problems, raws = [], [], {}
    if not files:
        msg = "k8s/ has no .yaml, .yml or .json files"
        if nested:
            msg += (f" directly inside it ({nested[0]} is in a subfolder, and "
                    f"`kubectl apply -f k8s/` does not read subfolders)")
        return None, [msg], {}
    for f in files:
        rel = os.path.relpath(f, root)
        text = read_text(f)
        raws[rel] = text
        try:
            loaded = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (line {mark.line + 1})" if mark else ""
            problems.append(f"{rel}{where} is not valid YAML: "
                            f"{short(str(getattr(exc, 'problem', exc)), 90)}")
            continue
        for i, doc in enumerate(loaded):
            if doc is None:
                continue
            if isinstance(doc, dict) and doc.get("kind") == "List":
                items = doc.get("items") or []
            else:
                items = [doc]
            for item in items:
                if not isinstance(item, dict) or not item.get("kind") or not item.get("apiVersion"):
                    problems.append(f"{rel}: document {i + 1} is not a Kubernetes object "
                                    f"(no kind or apiVersion)")
                    continue
                item["_file"] = rel
                docs.append(item)
    if nested:
        problems.append(f"{nested[0]} is in a subfolder of k8s/; `kubectl apply -f k8s/` "
                        f"does not read subfolders - move it up")
    return docs, problems, raws


def of_kind(docs, kind):
    return [d for d in docs if d.get("kind") == kind]


def name_of(doc):
    return dig(doc, "metadata", "name", default="") or ""


def pod_spec(dep):
    return dig(dep, "spec", "template", "spec", default={}) or {}


def all_containers(spec):
    out = []
    for field in ("initContainers", "containers", "ephemeralContainers"):
        out += [c for c in (spec.get(field) or []) if isinstance(c, dict)]
    return out


def split_image(ref):
    """'ghcr.io/o/r:tag@sha256:d' -> (repo, tag or None, digest or None)."""
    ref = (ref or "").strip()
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    last = ref.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        ref, tag = ref.rsplit(":", 1)
    return ref, tag, digest


def is_rescue(repo):
    return any(repo.lower() == r for r in RESCUE_IMAGES)


def app_container(dep, expected_repo):
    cs = [c for c in (pod_spec(dep).get("containers") or []) if isinstance(c, dict)]
    if not cs:
        return None
    for c in cs:
        repo = split_image(c.get("image"))[0].lower()
        if repo == (expected_repo or "").lower() or is_rescue(repo):
            return c
    return cs[0]


def label_selector(dep):
    ml = dig(dep, "spec", "selector", "matchLabels", default={}) or {}
    return ",".join(f"{k}={v}" for k, v in sorted(ml.items())) if isinstance(ml, dict) else ""


# ------------------------------------------------------------ quantities

def parse_quantity(q, kind):
    """CPU in cores or memory in bytes, from a Kubernetes quantity. None if unreadable."""
    if isinstance(q, (int, float)) and not isinstance(q, bool):
        return float(q)
    if not isinstance(q, str):
        return None
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)\s*", q)
    if not m:
        return None
    num, suf = float(m.group(1)), m.group(2)
    if kind == "cpu":
        return {"": num, "m": num / 1000}.get(suf)
    mult = {"": 1, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
            "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "m": 1e-3}
    return num * mult[suf] if suf in mult else None


# ------------------------------------------------------------ ports and env

def config_maps(docs):
    return {name_of(d): (d.get("data") or {}) for d in of_kind(docs, "ConfigMap")}


def port_source(container, docs):
    """Where the container's PORT comes from.

    -> {'how': 'configmap'|'literal'|'secret'|'field'|'missing', 'cm':..., 'key':...,
        'value': str or None, 'optional': bool, 'via': text}"""
    cms = config_maps(docs)
    found = None
    for e in container.get("env") or []:
        if not isinstance(e, dict) or e.get("name") != "PORT":
            continue
        if "value" in e:
            found = {"how": "literal", "value": str(e.get("value")), "via": "env value"}
        elif dig(e, "valueFrom", "configMapKeyRef"):
            ref = e["valueFrom"]["configMapKeyRef"]
            cm, key = ref.get("name"), ref.get("key")
            found = {"how": "configmap", "cm": cm, "key": key,
                     "value": None if cm not in cms else
                     (None if cms[cm].get(key) is None else str(cms[cm].get(key))),
                     "optional": bool(ref.get("optional")),
                     "via": f"configMapKeyRef {cm}/{key}"}
        elif dig(e, "valueFrom", "secretKeyRef"):
            found = {"how": "secret", "value": None, "via": "a Secret"}
        else:
            found = {"how": "field", "value": None, "via": "valueFrom"}
    if found:
        return found
    for ef in container.get("envFrom") or []:
        if not isinstance(ef, dict):
            continue
        prefix = ef.get("prefix") or ""
        if ef.get("configMapRef"):
            cm = ef["configMapRef"].get("name")
            for key, val in (cms.get(cm) or {}).items():
                if prefix + str(key) == "PORT":
                    found = {"how": "configmap", "cm": cm, "key": key,
                             "value": None if val is None else str(val),
                             "optional": bool(ef["configMapRef"].get("optional")),
                             "via": f"envFrom configMapRef {cm}"}
        if ef.get("secretRef"):
            pass            # a Secret cannot be searched statically; handled below
    return found or {"how": "missing", "value": None, "via": ""}


def other_config_values(container, spec, docs):
    """Settings the container gets from ConfigMaps, other than PORT."""
    cms = config_maps(docs)
    out = []
    for e in container.get("env") or []:
        if isinstance(e, dict) and e.get("name") != "PORT" and dig(e, "valueFrom", "configMapKeyRef"):
            out.append(f"env {e.get('name')}")
    for ef in container.get("envFrom") or []:
        if isinstance(ef, dict) and ef.get("configMapRef"):
            cm = ef["configMapRef"].get("name")
            prefix = ef.get("prefix") or ""
            out += [f"{prefix}{k} (envFrom {cm})" for k in (cms.get(cm) or {})
                    if prefix + str(k) != "PORT"]
    mounted = {m.get("name") for m in (container.get("volumeMounts") or []) if isinstance(m, dict)}
    for v in spec.get("volumes") or []:
        if not isinstance(v, dict) or v.get("name") not in mounted:
            continue
        if v.get("configMap"):
            out.append(f"file(s) from ConfigMap {v['configMap'].get('name')}")
        for src in dig(v, "projected", "sources", default=[]) or []:
            if isinstance(src, dict) and src.get("configMap"):
                out.append(f"file(s) from ConfigMap {src['configMap'].get('name')}")
    return out


def config_map_refs(container, spec):
    names = set()
    for e in container.get("env") or []:
        ref = dig(e, "valueFrom", "configMapKeyRef")
        if ref and not ref.get("optional"):
            names.add(ref.get("name"))
    for ef in container.get("envFrom") or []:
        if isinstance(ef, dict) and ef.get("configMapRef") and not ef["configMapRef"].get("optional"):
            names.add(ef["configMapRef"].get("name"))
    for v in spec.get("volumes") or []:
        if isinstance(v, dict) and v.get("configMap") and not v["configMap"].get("optional"):
            names.add(v["configMap"].get("name"))
    return {n for n in names if n}


def secret_refs(container, spec):
    """-> list of (secret name, key or None, optional, how) the app container uses."""
    refs = []
    for e in container.get("env") or []:
        ref = dig(e, "valueFrom", "secretKeyRef")
        if ref:
            refs.append((ref.get("name"), ref.get("key"), bool(ref.get("optional")),
                         f"env {e.get('name')}"))
    for ef in container.get("envFrom") or []:
        if isinstance(ef, dict) and ef.get("secretRef"):
            refs.append((ef["secretRef"].get("name"), None, bool(ef["secretRef"].get("optional")),
                         "envFrom"))
    mounted = {m.get("name") for m in (container.get("volumeMounts") or []) if isinstance(m, dict)}
    for v in spec.get("volumes") or []:
        if not isinstance(v, dict) or v.get("name") not in mounted:
            continue
        sources = []
        if v.get("secret"):
            sources.append((v["secret"].get("secretName"), v["secret"]))
        for src in dig(v, "projected", "sources", default=[]) or []:
            if isinstance(src, dict) and src.get("secret"):
                sources.append((src["secret"].get("name"), src["secret"]))
        for name, s in sources:
            items = [i.get("key") for i in (s.get("items") or []) if isinstance(i, dict)]
            if items:
                refs += [(name, k, bool(s.get("optional")), f"file {k}") for k in items]
            else:
                refs.append((name, None, bool(s.get("optional")), "mounted volume"))
    return refs


def container_port_named(container, name):
    for p in container.get("ports") or []:
        if isinstance(p, dict) and p.get("name") == name:
            return as_int(p.get("containerPort"))
    return None


def resolve_port(value, container):
    """A probe port or a Service targetPort -> the container port number, or None."""
    n = as_int(value)
    if n is not None:
        return n
    if isinstance(value, str):
        return container_port_named(container, value)
    return None


def probe_period(probe):
    return as_int(probe.get("periodSeconds"), 10) or 10


def probe_failures(probe):
    return as_int(probe.get("failureThreshold"), 3) or 3


def probe_timeout(probe):
    return as_int(probe.get("timeoutSeconds"), 1) or 1


def grace_seconds(spec, probe=None):
    g = as_int((probe or {}).get("terminationGracePeriodSeconds"))
    if g is None:
        g = as_int(spec.get("terminationGracePeriodSeconds"), 30)
    return 30 if g is None else g


def serving_services(docs, dep, container, port):
    """[(service, port entry)] for Services that select the Pods and target PORT."""
    labels = dig(dep, "spec", "template", "metadata", "labels", default={}) or {}
    out = []
    for svc in of_kind(docs, "Service"):
        sel = dig(svc, "spec", "selector", default={}) or {}
        if not sel or not isinstance(sel, dict) or dig(svc, "spec", "type") == "ExternalName":
            continue
        if any(labels.get(k) != v for k, v in sel.items()):
            continue
        for p in dig(svc, "spec", "ports", default=[]) or []:
            if not isinstance(p, dict):
                continue
            tp = p.get("targetPort", p.get("port"))
            if port is not None and resolve_port(tp, container) == port:
                out.append((svc, p))
    return out


def selecting_services(docs, dep):
    labels = dig(dep, "spec", "template", "metadata", "labels", default={}) or {}
    out = []
    for svc in of_kind(docs, "Service"):
        sel = dig(svc, "spec", "selector", default={}) or {}
        if sel and isinstance(sel, dict) and all(labels.get(k) == v for k, v in sel.items()):
            out.append(svc)
    return out


# ============================================================ the context

class Ctx(dict):
    """Everything the gates and blocks read. A dict, so the report is easy to build."""


def build_ctx(root, repo_root, slug, sha, date, subject):
    docs, load_problems, raws = load_manifests(root)
    expected = f"ghcr.io/{slug}".lower() if slug else None
    ctx = Ctx(root=root, repo_root=repo_root, slug=slug, expected_repo=expected,
              sha=sha, date=date, subject=subject, docs=docs, load_problems=load_problems,
              raws=raws, notes=[], live=None, dep=None, container=None)
    if docs:
        deps = of_kind(docs, "Deployment")
        if len(deps) == 1:
            ctx["dep"] = deps[0]
            ctx["container"] = app_container(deps[0], expected)
    return ctx


# ================================================================= gates

def gate_manifests(ctx):
    if ctx["docs"] is None:
        return False, "; ".join(ctx["load_problems"])
    problems = list(ctx["load_problems"])
    docs = ctx["docs"]
    for d in docs:
        k = d.get("kind")
        if k in IGNORED_KINDS:
            ctx["notes"].append(f"{d['_file']}: {k} {name_of(d)} is not applied by the marker "
                                f"(it is not part of M4)")
        elif k == "Secret":
            pass                                   # the 'no secrets' gate explains it
        elif k not in ALLOWED_KINDS:
            problems.append(f"{d['_file']}: kind {k} is not allowed in M4 (allowed: "
                            f"{', '.join(sorted(ALLOWED_KINDS))})")
    deps = of_kind(docs, "Deployment")
    if len(deps) != 1:
        problems.append(f"k8s/ must contain exactly one Deployment - your service - and it "
                        f"has {len(deps)}")
    if not of_kind(docs, "Service"):
        problems.append("k8s/ has no Service")
    if len(deps) == 1:
        spec = pod_spec(deps[0])
        if not (spec.get("containers") or []):
            problems.append("the Deployment has no containers")
        expected = ctx["expected_repo"]
        if not expected:
            problems.append("cannot work out your repository's name - run the marker with "
                            "--repo, or from a clone whose 'origin' is on GitHub")
        for c in all_containers(spec):
            repo = split_image(c.get("image"))[0]
            if expected and repo != expected and not is_rescue(repo.lower()):
                hint = (" - image names must be lowercase" if repo.lower() in (expected, *RESCUE_IMAGES)
                        else "")
                problems.append(f"container '{c.get('name')}' runs {c.get('image')!r}: every "
                                f"container must run your own image, {expected}, or the "
                                f"course's rescue image{hint}")
    if problems:
        return False, "; ".join(problems)
    kinds = sorted({d.get("kind") for d in docs})
    return True, f"{len(docs)} object(s) in k8s/: {', '.join(kinds)}"


def gate_safe(ctx):
    """Refuse anything that could reach the machine the marker runs on."""
    problems = []
    for d in ctx["docs"] or []:
        where = f"{d['_file']} ({d.get('kind')} {name_of(d)})"
        if d.get("kind") == "Service":
            if dig(d, "spec", "externalIPs"):
                problems.append(f"{where}: spec.externalIPs is not allowed")
            continue
        if d.get("kind") != "Deployment":
            continue
        spec = pod_spec(d)
        for f in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(f) is True:
                problems.append(f"{where}: {f}: true shares the machine's "
                                f"{f[4:].lower()} namespace - not allowed")
        if dig(spec, "securityContext", "sysctls"):
            problems.append(f"{where}: securityContext.sysctls is not allowed")
        for sc in (spec.get("securityContext") or {},):
            if dig(sc, "seccompProfile", "type") == "Unconfined":
                problems.append(f"{where}: seccompProfile Unconfined is not allowed")
            if sc.get("seLinuxOptions"):
                problems.append(f"{where}: seLinuxOptions are not allowed")
            if dig(sc, "appArmorProfile", "type") == "Unconfined":
                problems.append(f"{where}: appArmorProfile Unconfined is not allowed")
            if dig(sc, "windowsOptions", "hostProcess"):
                problems.append(f"{where}: hostProcess is not allowed")
        if spec.get("ephemeralContainers"):
            problems.append(f"{where}: ephemeralContainers cannot be declared in a template")
        for v in spec.get("volumes") or []:
            if not isinstance(v, dict):
                continue
            types = [k for k in v if k != "name"]
            bad = [t for t in types if t not in SAFE_VOLUME_TYPES]
            if bad:
                problems.append(f"{where}: volume '{v.get('name')}' is {bad[0]} - only "
                                f"configMap, secret, emptyDir, projected and downwardAPI "
                                f"volumes are allowed")
        for c in all_containers(spec):
            cn = f"{where} container '{c.get('name')}'"
            sc = c.get("securityContext") or {}
            if sc.get("privileged") is True:
                problems.append(f"{cn}: privileged: true is not allowed")
            if dig(sc, "capabilities", "add"):
                problems.append(f"{cn}: capabilities.add {dig(sc, 'capabilities', 'add')} is not "
                                f"allowed - drop capabilities, never add them")
            if sc.get("procMount") not in (None, "Default"):
                problems.append(f"{cn}: procMount {sc.get('procMount')} is not allowed")
            if dig(sc, "seccompProfile", "type") == "Unconfined":
                problems.append(f"{cn}: seccompProfile Unconfined is not allowed")
            if dig(sc, "appArmorProfile", "type") == "Unconfined":
                problems.append(f"{cn}: appArmorProfile Unconfined is not allowed")
            if sc.get("seLinuxOptions"):
                problems.append(f"{cn}: seLinuxOptions are not allowed")
            for p in c.get("ports") or []:
                if isinstance(p, dict) and p.get("hostPort"):
                    problems.append(f"{cn}: hostPort {p.get('hostPort')} is not allowed")
            for pr in PROBES:
                probe = c.get(pr) or {}
                if dig(probe, "httpGet", "host") or dig(probe, "tcpSocket", "host"):
                    problems.append(f"{cn}: {pr} sets a host - a probe checks your own Pod only")
            for hook in ("postStart", "preStop"):
                h = dig(c, "lifecycle", hook, default={}) or {}
                if dig(h, "httpGet", "host") or dig(h, "tcpSocket", "host"):
                    problems.append(f"{cn}: lifecycle.{hook} sets a host")
    if problems:
        seen, out = set(), []
        for p in problems:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return False, "; ".join(out)
    return True, "no host access, no privileged containers, no added capabilities"


def gate_no_secrets(ctx):
    problems = []
    for d in ctx["docs"] or []:
        if d.get("kind") == "Secret":
            problems.append(f"{d['_file']}: a Secret in Git is a leaked secret - base64 is not "
                            f"encryption. Delete this file; create '{SECRET_NAME}' on the cluster "
                            f"with kubectl (the marker creates it itself)")
        if d.get("kind") == "ConfigMap":
            for k, v in (d.get("data") or {}).items():
                if SECRETISH_NAME.search(str(k)) and isinstance(v, str) and len(v.strip()) >= 8 \
                        and not PLACEHOLDER.search(v):
                    problems.append(f"{d['_file']}: ConfigMap key '{k}' looks like a secret in "
                                    f"plain text - secrets belong in '{SECRET_NAME}'")
        if d.get("kind") == "Deployment":
            for c in all_containers(pod_spec(d)):
                for e in c.get("env") or []:
                    if not isinstance(e, dict) or "value" not in e:
                        continue
                    val = str(e.get("value") or "")
                    if SECRETISH_NAME.search(str(e.get("name", ""))) and len(val) >= 8 \
                            and not PLACEHOLDER.search(val):
                        problems.append(f"{d['_file']}: env {e.get('name')} has its value written "
                                        f"in the Deployment - take it from '{SECRET_NAME}' with "
                                        f"secretKeyRef")
    for rel, text in (ctx.get("raws") or {}).items():
        for line in text.splitlines():
            hit = scan_line(line)
            if hit:
                problems.append(f"{rel}: {hit}")
                break
    if problems:
        return False, "; ".join(dict.fromkeys(problems))
    return True, f"no Secret objects and no secret values in k8s/"


def gate_no_credentials(ctx):
    root = ctx["repo_root"]
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return False, "not a git repository, so the history cannot be checked"
    rc, out, _ = run(["git", "log", "-p", "--all", "--no-color"], cwd=root, timeout=180)
    if rc != 0:
        return False, "could not read the git history"
    hits = [h for h in (scan_line(l) for l in out.splitlines() if l.startswith("+")) if h]
    if hits:
        return False, "credential found in git history - " + "; ".join(sorted(set(hits))[:3])
    return True, "no credentials anywhere in the git history"


STATIC_GATES = [("manifests", gate_manifests), ("safe to run", gate_safe),
                ("no secrets in k8s/", gate_no_secrets),
                ("no credentials", gate_no_credentials)]


# ======================================================= static checks per block

def static_deploys(ctx):
    problems, dep, c = [], ctx["dep"], ctx["container"]
    image = c.get("image") or ""
    repo, tag, digest = split_image(image)
    if is_rescue(repo.lower()):
        problems.append(f"you deploy the course's rescue image {repo}: 'it deploys' needs YOUR "
                        f"image, {ctx['expected_repo']} - the other blocks can still score")
    elif not tag:
        problems.append(f"image {image!r} has no tag" + (" - name the commit with its SHA tag "
                        "(a digest may follow it)" if digest else " - it means :latest"))
    elif tag == "latest":
        problems.append("image tag 'latest' is not a version - use the full commit SHA that "
                        "your M3 pipeline tagged")
    else:
        core = tag[4:] if tag.startswith("sha-") else tag
        if not re.fullmatch(r"[0-9a-f]{7,40}", core):
            problems.append(f"image tag {tag!r} is not a commit SHA - use the full 40-character "
                            f"SHA your M3 pipeline tagged")
        elif ctx["repo_root"]:
            rc, out, _ = run(["git", "rev-parse", "--verify", "--quiet", f"{core}^{{commit}}"],
                             cwd=ctx["repo_root"])
            if rc != 0:
                problems.append(f"image tag {tag} is not a commit in your repository - copy the "
                                f"SHA from `git log`, do not type it")
            elif len(core) < 40:
                ctx["notes"].append(f"your image tag is a short SHA ({core}); M3 tagged the full "
                                    f"40 characters - if the pull fails, that is why")
    replicas = dig(dep, "spec", "replicas")
    n = 1 if replicas is None else as_int(replicas)
    if n is None or n < MIN_REPLICAS:
        problems.append(f"replicas is {replicas if replicas is not None else 'not set (1)'} - "
                        f"run at least {MIN_REPLICAS} copies, so one can fail while another "
                        f"serves")
    spec = pod_spec(dep)
    csc, psc = c.get("securityContext") or {}, spec.get("securityContext") or {}
    nonroot = csc.get("runAsNonRoot", psc.get("runAsNonRoot"))
    if nonroot is not True:
        problems.append("runAsNonRoot is not true - set securityContext.runAsNonRoot: true "
                        "(your image runs as USER 1000 since week 5, so it passes)")
    uid = csc.get("runAsUser", psc.get("runAsUser"))
    if as_int(uid) == 0:
        problems.append("runAsUser: 0 is root")
    return problems, f"{image}, {n} replicas, runAsNonRoot"


def static_serves(ctx):
    problems, dep, c = [], ctx["dep"], ctx["container"]
    port = as_int(port_source(c, ctx["docs"]).get("value"))
    selecting = selecting_services(ctx["docs"], dep)
    labels = dig(dep, "spec", "template", "metadata", "labels", default={}) or {}
    if not selecting:
        sels = [dig(s, "spec", "selector", default={}) for s in of_kind(ctx["docs"], "Service")]
        problems.append(f"no Service selects your Pods: their labels are {labels}, the Service "
                        f"selector(s) are {sels}. A Service that matches nothing is accepted "
                        f"without an error - and has no endpoints")
        return problems, ""
    if port is None:
        problems.append("the marker cannot tell your PORT (see 'it is configured'), so it "
                        "cannot check which port the Service targets")
        return problems, ""
    serving = serving_services(ctx["docs"], dep, c, port)
    if not serving:
        tps = [p.get("targetPort", p.get("port")) for s in selecting
               for p in (dig(s, "spec", "ports", default=[]) or []) if isinstance(p, dict)]
        problems.append(f"Service '{name_of(selecting[0])}' selects your Pods, but no targetPort "
                        f"reaches your PORT {port} (targetPorts: {tps})")
        return problems, ""
    svc, p = serving[0]
    return problems, f"Service {name_of(svc)} port {p.get('port')} -> {port}"


def probe_problems(name, probe, c, port, budget):
    if not probe:
        return [f"no {name}"]
    out = []
    http = probe.get("httpGet")
    if not http:
        kind = next((k for k in ("tcpSocket", "exec", "grpc") if probe.get(k)), "no handler")
        return [f"{name} uses {kind} - it must be httpGet /healthz (a {kind} probe keeps passing "
                f"while your app is frozen)"]
    if http.get("path") != "/healthz":
        out.append(f"{name} calls {http.get('path')!r}, not /healthz")
    if str(http.get("scheme", "HTTP")).upper() != "HTTP":
        out.append(f"{name} uses {http.get('scheme')} - your app speaks plain HTTP")
    p = resolve_port(http.get("port"), c)
    if port is not None and p != port:
        out.append(f"{name} checks port {http.get('port')!r}"
                   + (f" (= {p})" if p is not None and p != http.get("port") else "")
                   + f", but your app listens on PORT {port}")
    if as_int(probe.get("initialDelaySeconds"), 0) > INITIAL_DELAY_MAX:
        out.append(f"{name} initialDelaySeconds {probe.get('initialDelaySeconds')} is over "
                   f"{INITIAL_DELAY_MAX} - use a startupProbe for a slow start")
    if probe_timeout(probe) > TIMEOUT_MAX:
        out.append(f"{name} timeoutSeconds {probe_timeout(probe)} is over {TIMEOUT_MAX}")
    total = probe_period(probe) * probe_failures(probe)
    if total > budget:
        out.append(f"{name} needs {probe_period(probe)} x {probe_failures(probe)} = {total} s to "
                   f"notice a dead app; keep periodSeconds x failureThreshold at or under "
                   f"{budget}")
    return out


def static_probes(ctx):
    dep, c = ctx["dep"], ctx["container"]
    port = as_int(port_source(c, ctx["docs"]).get("value"))
    problems = probe_problems("livenessProbe", c.get("livenessProbe"), c, port, LIVENESS_BUDGET)
    problems += probe_problems("readinessProbe", c.get("readinessProbe"), c, port, READINESS_BUDGET)
    sp = c.get("startupProbe")
    if sp:
        handler = sp.get("httpGet") or sp.get("tcpSocket")
        if not handler:
            problems.append("startupProbe must use httpGet or tcpSocket on your PORT")
        elif port is not None and resolve_port(handler.get("port"), c) != port:
            problems.append(f"startupProbe checks port {handler.get('port')!r}, not PORT {port}")
        total = probe_period(sp) * probe_failures(sp)
        if total > STARTUP_BUDGET:
            problems.append(f"startupProbe allows {total} s; keep it at or under {STARTUP_BUDGET}")
    spec = pod_spec(dep)
    if grace_seconds(spec, c.get("livenessProbe")) > GRACE_MAX:
        problems.append(f"terminationGracePeriodSeconds {grace_seconds(spec, c.get('livenessProbe'))} "
                        f"is over {GRACE_MAX}")
    if port is None:
        problems.append("the marker cannot tell your PORT, so it cannot check the probes' port")
    return problems, "liveness and readiness: httpGet /healthz on PORT"


def static_configured(ctx):
    problems, dep, c, docs = [], ctx["dep"], ctx["container"], ctx["docs"]
    spec = pod_spec(dep)
    src = port_source(c, docs)
    cms = config_maps(docs)
    if src["how"] == "literal":
        problems.append(f"PORT is written in the Deployment (value: {src['value']!r}) - take it "
                        f"from your ConfigMap with configMapKeyRef or envFrom")
    elif src["how"] == "missing":
        problems.append("the container gets no PORT from a ConfigMap (check the envFrom prefix, "
                        "or add env PORT with configMapKeyRef)")
    elif src["how"] in ("secret", "field"):
        problems.append(f"PORT comes from {src['via']} - it must come from a ConfigMap")
    elif src["how"] == "configmap":
        if src["cm"] not in cms:
            problems.append(f"PORT comes from ConfigMap '{src['cm']}', which is not in k8s/")
        elif src["value"] is None:
            problems.append(f"ConfigMap '{src['cm']}' has no key '{src['key']}'")
        elif as_int(src["value"]) is None or not 1 <= as_int(src["value"]) <= 65535:
            problems.append(f"PORT in ConfigMap '{src['cm']}' is {src['value']!r} - it must be a "
                            f"port number, as a quoted string")
        if src.get("optional"):
            problems.append("PORT is marked optional - remove 'optional: true'")
    for name in sorted(config_map_refs(c, spec) - set(cms)):
        problems.append(f"the Deployment reads ConfigMap '{name}', which is not in k8s/")
    port = as_int(src.get("value"))
    words = " ".join(str(x) for x in (c.get("command") or []) + (c.get("args") or []))
    if port is not None and (re.search(rf"(?<![\d$(]){port}(?!\d)", words)
                             or re.search(r"(?i)--?port\b|\bPORT=", words)):
        problems.append(f"command/args set the port themselves ({short(words, 60)!r}) - let the "
                        f"app read $PORT")
    others = other_config_values(c, spec, docs)
    if not others:
        problems.append("only PORT comes from a ConfigMap - move at least one more setting there")
    refs = secret_refs(c, spec)
    mine = [r for r in refs if r[0] == SECRET_NAME]
    for name, key, optional, how in refs:
        if name != SECRET_NAME and not optional:
            problems.append(f"the container reads Secret '{name}' ({how}); only '{SECRET_NAME}' "
                            f"exists in the marker's cluster - rename it")
    if not mine:
        problems.append(f"no value comes from the Secret '{SECRET_NAME}' - add an env variable "
                        f"with secretKeyRef name: {SECRET_NAME}")
    elif all(r[2] for r in mine):
        problems.append(f"every reference to '{SECRET_NAME}' is optional: true - then the Pod "
                        f"starts without it. Remove 'optional'")
    res = c.get("resources") or {}
    for part in ("requests", "limits"):
        for r in ("cpu", "memory"):
            v = dig(res, part, r)
            if v is None:
                problems.append(f"no {part}.{r}")
            elif parse_quantity(v, r) is None:
                problems.append(f"{part}.{r} {v!r} is not a quantity I can read")
    req_cpu = parse_quantity(dig(res, "requests", "cpu"), "cpu")
    req_mem = parse_quantity(dig(res, "requests", "memory"), "memory")
    if (req_cpu or 0) > 1 or (req_mem or 0) > 1024 ** 3:
        ctx["notes"].append("your requests are over 1 CPU or 1 GiB per Pod - on a small node the "
                            "Pods may stay Pending")
    detail = (f"PORT from {src.get('via')}, also {', '.join(others[:3])}; "
              f"{SECRET_NAME} ({', '.join(sorted({r[3] for r in mine}))}); requests and limits"
              if not problems else "")
    return problems, detail


STATIC = {"it deploys": static_deploys, "it serves": static_serves,
          "the probes are real": static_probes, "it is configured, not hardcoded": static_configured}
BLOCK_NAMES = list(STATIC)


# ================================================================== the cluster

class Cluster:
    """The kubectl, kind and docker commands the live checks need. The tests replace the
    three programs with fakes on PATH, so every command below is exercised."""

    def __init__(self, context, clock=None):
        self.context = context
        self.clock = clock or Clock()

    def kubectl(self, *args, input=None, timeout=90):
        return run(["kubectl", "--context", self.context, *args], input=input, timeout=timeout)

    def get_json(self, *args, timeout=60):
        rc, out, err = self.kubectl(*args, "-o", "json", timeout=timeout)
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def reachable(self):
        rc, out, _ = self.kubectl("get", "--raw", "/readyz", timeout=30)
        return rc == 0 and "ok" in out

    def apply_stdin(self, ns, obj):
        return self.kubectl("-n", ns, "apply", "-f", "-", input=json.dumps(obj))

    def namespace(self, ns):
        return self.kubectl("apply", "-f", "-", input=json.dumps({
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": ns, "labels": {
                "pod-security.kubernetes.io/enforce": "baseline",
                "pod-security.kubernetes.io/enforce-version": "latest",
                "pod-security.kubernetes.io/warn": "restricted",
                "pod-security.kubernetes.io/warn-version": "latest"}}}))

    def apply_dir(self, ns, folder, dry_run=False):
        args = ["-n", ns, "apply", "-f", folder]
        if dry_run:
            args.insert(3, "--dry-run=server")
        return self.kubectl(*args, timeout=120)

    def rollout(self, ns, name, seconds):
        return self.kubectl("-n", ns, "rollout", "status", f"deployment/{name}",
                            f"--timeout={seconds}s", timeout=seconds + 30)

    def pods(self, ns, selector):
        data = self.get_json("-n", ns, "get", "pods", "-l", selector)
        items = (data or {}).get("items") or []
        return [p for p in items if not dig(p, "metadata", "deletionTimestamp")]

    def slices(self, ns, service):
        data = self.get_json("-n", ns, "get", "endpointslices",
                             "-l", f"kubernetes.io/service-name={service}")
        return None if data is None else (data.get("items") or [])

    def healthz(self, ns, service, port):
        rc, out, err = self.kubectl("get", "--raw",
                                    f"/api/v1/namespaces/{ns}/services/{service}:{port}/proxy/healthz",
                                    timeout=20)
        return rc == 0, short(out if rc == 0 else err, 160)

    def events(self, ns):
        data = self.get_json("-n", ns, "get", "events")
        items = (data or {}).get("items") or []
        return sorted(items, key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")

    def logs(self, ns, pod, container, previous=False):
        args = ["-n", ns, "logs", pod, "-c", container, "--tail=15"]
        if previous:
            args.append("--previous")
        rc, out, err = self.kubectl(*args, timeout=30)
        return short(out if rc == 0 else "", 300)


class Kind:
    def exists(self, name):
        rc, out, _ = run(["kind", "get", "clusters"], timeout=30)
        return rc == 0 and name in out.split()

    def create(self, name):
        return run(["kind", "create", "cluster", "--name", name, "--image", KIND_NODE_IMAGE,
                    "--wait", "180s"], timeout=600)

    def delete(self, name):
        run(["kind", "delete", "cluster", "--name", name], timeout=240)


# Runs INSIDE the kind node (a Docker container), as root, with sh. It sends a signal to
# every process of one container: the cgroup lists them all, whatever PID 1 is.
FREEZE_SCRIPT = r'''
pid="$1"; sig="$2"
cg=$(sed -n 's/^0:://p' "/proc/$pid/cgroup" 2>/dev/null)
if [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then
  pids=$(cat "/sys/fs/cgroup$cg/cgroup.procs")
else
  pids="$pid"
fi
kill -"$sig" $pids && echo $pids
'''


class NodeShell:
    """Reach a kind node with `docker exec` - kind nodes are containers."""

    def container_pid(self, node, container_id):
        rc, out, err = run(["docker", "exec", node, "crictl", "inspect", container_id], timeout=60)
        if rc != 0:
            return None, short(err or out, 160)
        try:
            return int(json.loads(out)["info"]["pid"]), ""
        except (ValueError, KeyError, TypeError):
            return None, "crictl inspect did not report a pid"

    def signal(self, node, pid, sig):
        rc, out, err = run(["docker", "exec", node, "sh", "-c", FREEZE_SCRIPT, "_", str(pid), sig],
                           timeout=60)
        return rc == 0, short(out if rc == 0 else err, 160)


# ================================================================ the live checks

def sanitize(docs, replicas_at_least=MIN_REPLICAS):
    """The copy of k8s/ the marker applies: only allowed kinds, no namespace, no
    server-set fields, and at least MIN_REPLICAS copies (a student with 1 replica loses
    'it deploys' for it, not the other blocks)."""
    out, notes = [], []
    for d in docs:
        if d.get("kind") not in ALLOWED_KINDS:
            continue
        d = copy.deepcopy(d)
        d.pop("_file", None)
        d.pop("status", None)
        md = d.setdefault("metadata", {})
        if md.get("namespace"):
            notes.append(f"{d.get('kind')} {md.get('name')}: metadata.namespace "
                         f"'{md['namespace']}' is ignored - the marker chooses the namespace")
        for f in ("namespace", "resourceVersion", "uid", "creationTimestamp", "managedFields",
                  "selfLink", "generation"):
            md.pop(f, None)
        if d.get("kind") == "Deployment":
            r = as_int(dig(d, "spec", "replicas"), None)
            if r is None or r < replicas_at_least:
                d.setdefault("spec", {})["replicas"] = replicas_at_least
        out.append(d)
    return out, notes


def write_docs(docs, folder):
    os.makedirs(folder, exist_ok=True)
    for i, d in enumerate(docs):
        fn = f"{i:02d}-{d.get('kind', 'x').lower()}-{re.sub(r'[^a-z0-9-]', '', name_of(d).lower())}.json"
        with open(os.path.join(folder, fn), "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    return folder


def rewrite_port(docs, dep_name, container_name, cm_name, cm_key, old, new):
    """Deploy with a PORT the student did not choose: the ConfigMap value, and every
    number that must follow it (containerPort, probe ports, Service targetPorts)."""
    out = copy.deepcopy(docs)
    for d in out:
        if d.get("kind") == "ConfigMap" and name_of(d) == cm_name:
            d.setdefault("data", {})[cm_key] = str(new)
    dep = next(d for d in out if d.get("kind") == "Deployment" and name_of(d) == dep_name)
    labels = dig(dep, "spec", "template", "metadata", "labels", default={}) or {}
    for c in dep["spec"]["template"]["spec"].get("containers") or []:
        if c.get("name") != container_name:
            continue
        for p in c.get("ports") or []:
            if as_int(p.get("containerPort")) == old:
                p["containerPort"] = new
        for pr in PROBES:
            for h in ("httpGet", "tcpSocket"):
                handler = dig(c, pr, h)
                if handler and as_int(handler.get("port")) == old:
                    handler["port"] = new
    for svc in out:
        if svc.get("kind") != "Service":
            continue
        sel = dig(svc, "spec", "selector", default={}) or {}
        if not sel or any(labels.get(k) != v for k, v in sel.items()):
            continue
        for p in dig(svc, "spec", "ports", default=[]) or []:
            if "targetPort" in p and as_int(p["targetPort"]) == old:
                p["targetPort"] = new
            elif "targetPort" not in p and as_int(p.get("port")) == old:
                p["targetPort"] = new
    return out


def secret_object(keys):
    return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME},
            "stringData": {k: pysecrets.token_hex(16) for k in sorted(keys)}}


def secret_keys(container, spec):
    keys = {k for n, k, _, _ in secret_refs(container, spec) if n == SECRET_NAME and k}
    if any(n == SECRET_NAME and not k for n, k, _, _ in secret_refs(container, spec)):
        keys.add(DEFAULT_SECRET_KEY)
    return keys or {DEFAULT_SECRET_KEY}


def container_status(pod, name):
    for cs in dig(pod, "status", "containerStatuses", default=[]) or []:
        if cs.get("name") == name:
            return cs
    return {}


def pod_ready(pod):
    return any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in dig(pod, "status", "conditions", default=[]) or [])


def diagnose(cl, ns, selector, container_name):
    """One line on why the Pods are not ready: the words from Kubernetes itself."""
    pods = cl.pods(ns, selector)
    if not pods:
        evs = [e for e in cl.events(ns) if e.get("type") == "Warning"]
        return ("no Pods were created" + (f": {short(evs[-1].get('message'), 200)}" if evs else ""))
    bad = [p for p in pods if not pod_ready(p)] or pods
    p = bad[0]
    pname = name_of(p)
    cs = container_status(p, container_name)
    waiting = dig(cs, "state", "waiting", default={}) or {}
    last = dig(cs, "lastState", "terminated", default={}) or {}
    evs = [e for e in cl.events(ns) if dig(e, "involvedObject", "name") == pname
           and e.get("type") == "Warning"]
    ev = short(evs[-1].get("message"), 220) if evs else ""
    phase = dig(p, "status", "phase", default="?")
    if phase == "Pending" and not cs:
        return f"Pod {pname} is Pending{': ' + ev if ev else ''}"
    reason = waiting.get("reason") or (dig(cs, "state", "terminated", "reason")) or \
        ("Running but not ready" if dig(cs, "state", "running") else phase)
    parts = [f"Pod {pname}: {reason}"]
    if waiting.get("message"):
        parts.append(short(waiting["message"], 220))
    elif ev:
        parts.append(ev)
    if last:
        parts.append(f"last exit code {last.get('exitCode')} ({last.get('reason')})")
        logs = cl.logs(ns, pname, container_name, previous=True)
        if logs:
            parts.append(f"its last output: {logs}")
    return " - ".join(parts)


class Live:
    """Runs the cluster part once and remembers what it saw, for the four blocks."""

    def __init__(self, ctx, cluster, node=None, clock=None, rng=None):
        self.ctx, self.cl = ctx, cluster
        self.node = node or NodeShell()
        self.clock = clock or cluster.clock
        self.rng = rng or random.Random()
        self.dep, self.c = ctx["dep"], ctx["container"]
        self.selector = label_selector(self.dep)
        self.dep_name = name_of(self.dep)
        self.cname = self.c.get("name")
        self.port = as_int(port_source(self.c, ctx["docs"]).get("value"))
        serving = serving_services(ctx["docs"], self.dep, self.c, self.port) if self.port else []
        self.svc, self.svc_port = ((name_of(serving[0][0]), serving[0][1].get("port"))
                                   if serving else (None, None))
        self.infra = None               # set when the cluster itself failed us
        self.dry = None                 # (ok, detail)
        self.rolled = None              # (ok, detail, seconds)
        self.tmp = tempfile.mkdtemp(prefix="m4-live-")

    # --------------------------------------------------------------- setup
    def setup(self):
        if not self.cl.reachable():
            self.infra = "the cluster did not answer"
            return
        docs, notes = sanitize(self.ctx["docs"])
        self.ctx["notes"].extend(notes)
        self.docs = docs
        folder = write_docs(docs, os.path.join(self.tmp, "main"))
        rc, out, err = self.cl.namespace(NS_MAIN)
        if rc != 0:
            self.infra = f"could not create namespace {NS_MAIN}: {short(err)}"
            return
        rc, out, err = self.cl.apply_stdin(NS_MAIN, secret_object(secret_keys(self.c, pod_spec(self.dep))))
        if rc != 0:
            self.infra = f"could not create {SECRET_NAME}: {short(err)}"
            return
        rc, out, err = self.cl.apply_dir(NS_MAIN, folder, dry_run=True)
        self.dry = (rc == 0, short(out if rc == 0 else err, 400))
        if rc != 0:
            return
        start = self.clock.now()
        rc, out, err = self.cl.apply_dir(NS_MAIN, folder)
        warns = sorted({short(l, 300) for l in err.splitlines() if "PodSecurity" in l})
        for w in warns[:1]:
            self.ctx["notes"].append(f"Pod Security 'restricted' (advice, not marked): {w}")
        if rc != 0:
            self.rolled = (False, f"kubectl apply failed: {short(err, 300)}", 0)
            return
        rc, out, err = self.cl.rollout(NS_MAIN, self.dep_name, ROLLOUT_SECONDS)
        secs = int(self.clock.now() - start)
        if rc != 0:
            self.rolled = (False, f"not all Pods were ready after {ROLLOUT_SECONDS} s - "
                                  f"{diagnose(self.cl, NS_MAIN, self.selector, self.cname)}", secs)
            return
        self.rolled = (True, f"rolled out in about {secs} s", secs)

    def ready_pods(self, ns=NS_MAIN):
        return [p for p in self.cl.pods(ns, self.selector) if pod_ready(p)]

    # ------------------------------------------------------------ block 1
    def deploys(self):
        ok, detail, _ = self.rolled
        if not ok:
            return False, detail
        dep = self.cl.get_json("-n", NS_MAIN, "get", "deployment", self.dep_name) or {}
        want = as_int(dig(dep, "spec", "replicas"), 0)
        have = as_int(dig(dep, "status", "readyReplicas"), 0)
        if not want or have < want:
            return False, f"{have}/{want} Pods ready after the rollout"
        return True, f"{detail}: {have}/{want} Pods ready"

    # ------------------------------------------------------------ block 2
    def endpoint_state(self, ns=NS_MAIN, service=None):
        """-> {pod name: ready?} from the Service's EndpointSlices, or None."""
        slices = self.cl.slices(ns, service or self.svc)
        if slices is None:
            return None
        state = {}
        for s in slices:
            for ep in s.get("endpoints") or []:
                ref = ep.get("targetRef") or {}
                if ref.get("kind") == "Pod":
                    state[ref.get("name")] = dig(ep, "conditions", "ready") is not False
        return state

    def slice_ports(self, service):
        ports = set()
        for s in self.cl.slices(NS_MAIN, service) or []:
            for p in s.get("ports") or []:
                if as_int(p.get("port")) is not None:
                    ports.add(as_int(p.get("port")))
        return ports

    def serves(self):
        if not self.rolled[0]:
            return False, "nothing was running - see 'it deploys'"
        pods = {name_of(p) for p in self.ready_pods()}
        state = self.endpoint_state()
        if state is None:
            return None, "could not read the EndpointSlices"
        ready = {n for n, r in state.items() if r}
        if not ready:
            return False, (f"Service {self.svc} has no ready endpoints - its selector finds none "
                           f"of your Pods")
        if ready != pods:
            return False, (f"Service {self.svc} lists {len(ready)} ready endpoint(s), your "
                           f"Deployment has {len(pods)} ready Pod(s)")
        ports = self.slice_ports(self.svc)
        if ports and self.port not in ports:
            return False, f"the Service's endpoints use port(s) {sorted(ports)}, not PORT {self.port}"
        good, last = 0, ""
        for _ in range(HEALTHZ_TRIES):
            ok, last = self.cl.healthz(NS_MAIN, self.svc, self.svc_port)
            good += ok
        if good < HEALTHZ_TRIES:
            return False, (f"/healthz through Service {self.svc}:{self.svc_port} answered "
                           f"{good}/{HEALTHZ_TRIES} times - last error: {last}")
        return True, (f"Service {self.svc}:{self.svc_port} has all {len(pods)} Pods as ready "
                      f"endpoints on port {self.port}; /healthz answered {good}/{HEALTHZ_TRIES} "
                      f"times through it")

    # ------------------------------------------------------------ block 3
    def served_while_out(self):
        """3 calls through the Service; one retry round for slow endpoint propagation."""
        good = 0
        for attempt in range(2):
            self.clock.sleep(POLL_SECONDS + attempt)
            good = sum(self.cl.healthz(NS_MAIN, self.svc, self.svc_port)[0] for _ in range(3))
            if good == 3:
                break
        return good

    def probes(self):
        if not self.rolled[0]:
            return False, "nothing was running - see 'it deploys'"
        if not self.svc:
            return False, "the freeze test needs a Service that targets your PORT - see 'it serves'"
        pods = sorted(self.ready_pods(), key=name_of)
        if len(pods) < 2:
            return False, "the freeze test needs at least 2 ready Pods"
        target = pods[0]
        pname, node = name_of(target), dig(target, "spec", "nodeName")
        cs = container_status(target, self.cname)
        cid = re.sub(r"^\w+://", "", cs.get("containerID") or "")
        before = as_int(cs.get("restartCount"), 0)
        lp, rp = self.c.get("livenessProbe") or {}, self.c.get("readinessProbe") or {}
        spec = pod_spec(self.dep)
        # wait until both probes are running (initialDelaySeconds is at most 60)
        delay = max(as_int(lp.get("initialDelaySeconds"), 0), as_int(rp.get("initialDelaySeconds"), 0))
        if delay:
            self.clock.sleep(min(delay, INITIAL_DELAY_MAX) + 2)
        pid, why = self.node.container_pid(node, cid)
        if pid is None:
            return None, f"could not find the container's process on the node ({why})"
        ok, frozen = self.node.signal(node, pid, "STOP")
        if not ok:
            return None, f"could not freeze the container ({frozen})"
        t0 = self.clock.now()
        ready_bound = probe_period(rp) * probe_failures(rp) + probe_timeout(rp) + 20
        live_bound = (probe_period(lp) * probe_failures(lp) + probe_timeout(lp)
                      + grace_seconds(spec, lp) + 30)
        left = served = restarted = None
        failed_during = 0
        try:
            while True:
                now = self.clock.now() - t0
                pod = next((p for p in self.cl.pods(NS_MAIN, self.selector)
                            if name_of(p) == pname), None)
                count = as_int(container_status(pod or {}, self.cname).get("restartCount"), 0)
                if pod is None:
                    return False, f"Pod {pname} disappeared during the test"
                if count > before:
                    restarted = now
                    break
                if left is None:
                    state = self.endpoint_state()
                    if state is not None and (state.get(pname) is False or pname not in state):
                        left = now
                        served = self.served_while_out()
                        failed_during = 3 - served
                if left is None and now > ready_bound:
                    return False, (f"Pod {pname} was frozen for {int(now)} s and stayed in "
                                   f"Service {self.svc}'s ready endpoints: your readinessProbe did "
                                   f"not notice a frozen app")
                if now > live_bound:
                    return False, (f"Pod {pname} was frozen for {int(now)} s and its container "
                                   f"was never restarted: your livenessProbe did not notice")
                self.clock.sleep(POLL_SECONDS)
        finally:
            if restarted is None:
                self.node.signal(node, pid, "CONT")
        if left is None:
            return False, (f"the container was restarted after {int(restarted)} s, but readiness "
                           f"never took Pod {pname} out of the Service first - make readiness "
                           f"react faster than liveness")
        if failed_during:
            return False, (f"while {pname} was frozen and out of the endpoints, /healthz through "
                           f"the Service failed {failed_during}/3 times")
        sp = self.c.get("startupProbe")
        back_bound = restarted + 90 + (probe_period(sp) * probe_failures(sp) if sp else 0)
        while True:
            now = self.clock.now() - t0
            pod = next((p for p in self.cl.pods(NS_MAIN, self.selector) if name_of(p) == pname), None)
            if pod and pod_ready(pod):
                back = now
                break
            if now > back_bound:
                return False, (f"Pod {pname} was restarted after {int(restarted)} s but was not "
                               f"ready again within {int(back_bound - restarted)} s")
            self.clock.sleep(POLL_SECONDS)
        return True, (f"froze Pod {pname}: readiness took it out of the endpoints after about "
                      f"{int(left)} s while the Service kept answering ({served}/3); liveness "
                      f"restarted it after about {int(restarted)} s; ready again after about "
                      f"{int(back)} s")

    # ------------------------------------------------------------ block 4
    def configured(self):
        if not self.svc:
            return False, "needs a Service that targets your PORT - see 'it serves'"
        src = port_source(self.c, self.ctx["docs"])
        old = as_int(src.get("value"))
        new = old
        while new == old:
            new = self.rng.randint(20000, 29999)
        docs = rewrite_port(self.docs, self.dep_name, self.cname, src["cm"], src["key"], old, new)
        folder = write_docs(docs, os.path.join(self.tmp, "config"))
        rc, _, err = self.cl.namespace(NS_CONFIG)
        if rc != 0:
            return None, f"could not create namespace {NS_CONFIG}: {short(err)}"
        rc, _, err = self.cl.apply_stdin(NS_CONFIG, secret_object(secret_keys(self.c, pod_spec(self.dep))))
        if rc != 0:
            return None, f"could not create {SECRET_NAME} in {NS_CONFIG}: {short(err)}"
        rc, _, err = self.cl.apply_dir(NS_CONFIG, folder)
        if rc != 0:
            return False, f"with PORT={new}, kubectl apply failed: {short(err)}"
        rc, _, _ = self.cl.rollout(NS_CONFIG, self.dep_name, ROLLOUT_SECONDS)
        if rc != 0:
            return False, (f"the marker set PORT={new} in your ConfigMap '{src['cm']}' (and moved "
                           f"containerPort, probe ports and targetPort with it); the Pods were not "
                           f"ready after {ROLLOUT_SECONDS} s - is a port hardcoded in the app, "
                           f"command or args? "
                           f"{diagnose(self.cl, NS_CONFIG, self.selector, self.cname)}")
        ok, last = self.cl.healthz(NS_CONFIG, self.svc, self.svc_port)
        if not ok:
            return False, (f"with PORT={new}, the Pods started but /healthz through Service "
                           f"{self.svc} failed: {last}")
        return True, (f"with PORT={new} written into your ConfigMap and a new random "
                      f"{SECRET_NAME}, it rolled out and answered /healthz through the Service")


# ================================================================ marking

def mark(ctx, live_mode, cluster=None, kind=None, cluster_name=None, create=False,
         keep=False, node=None, clock=None, rng=None):
    rep = {"repo": ctx["slug"], "commit": {"sha": ctx["sha"], "date": ctx["date"],
                                          "subject": ctx["subject"]},
           "mode": "live" if live_mode else "static", "gates": [], "blocks": [],
           "skipped": [], "score": 0, "capped": False, "notes": ctx["notes"]}
    for name, fn in STATIC_GATES:
        ok, detail = fn(ctx)
        rep["gates"].append({"name": name, "ok": ok, "detail": detail})
        rep["capped"] = rep["capped"] or not ok
        if name == "manifests" and not ok:
            break                      # nothing below can be read without the manifests
    if rep["capped"]:
        return rep
    static = {}
    for name in BLOCK_NAMES:
        problems, detail = STATIC[name](ctx)
        static[name] = (problems, detail)
    if not live_mode:
        rep["notes"].append("dry run: not checked - `kubectl apply --dry-run=server` needs a "
                            "cluster (--kind, or your PR job)")
        for name in BLOCK_NAMES:
            problems, detail = static[name]
            if problems:
                rep["blocks"].append({"name": name, "points": 0, "detail": "; ".join(problems)})
            else:
                rep["skipped"].append({"name": name, "detail": f"static checks pass ({detail}); "
                                                              f"live checks not run - no cluster"})
        return rep

    live, created = None, False
    try:
        if create:
            print(f"  creating a throwaway kind cluster '{cluster_name}' (about a minute)...",
                  file=sys.stderr)
            rc, _, err = kind.create(cluster_name)
            if rc != 0:
                infra = f"could not create the kind cluster: {short(err, 200)}"
                return _not_checked(rep, infra)
            created = True
        live = Live(ctx, cluster, node=node, clock=clock, rng=rng)
        live.setup()
        if live.infra:
            return _not_checked(rep, live.infra)
        ok, detail = live.dry
        rep["gates"].append({"name": "dry run", "ok": ok,
                             "detail": "kubectl apply --dry-run=server accepts every file"
                             if ok else f"kubectl apply --dry-run=server refused: {detail}"})
        if not ok:
            rep["capped"] = True
            return rep
        runners = {"it deploys": live.deploys, "it serves": live.serves,
                   "the probes are real": live.probes,
                   "it is configured, not hardcoded": live.configured}
        for name in BLOCK_NAMES:
            problems, sdetail = static[name]
            if problems:
                rep["blocks"].append({"name": name, "points": 0, "detail": "; ".join(problems)})
                continue
            if name == "it is configured, not hardcoded" and not live.rolled[0]:
                pass                   # still try: a different PORT is a different deployment
            ok, detail = runners[name]()
            if ok is None:
                rep["skipped"].append({"name": name, "detail": f"NOT CHECKED: {detail}"})
            else:
                rep["blocks"].append({"name": name, "points": 25 if ok else 0, "detail": detail})
                rep["score"] += 25 if ok else 0
        return rep
    finally:
        if live:
            shutil.rmtree(live.tmp, ignore_errors=True)
        if created and not keep:
            kind.delete(cluster_name)


def _not_checked(rep, why):
    rep["notes"].append(f"dry run: not checked - {why}")
    for name in BLOCK_NAMES:
        rep["skipped"].append({"name": name, "detail": f"NOT CHECKED: {why}"})
    return rep


def print_report(ctx, rep):
    print(f"\n  M4 for {ctx['slug'] or ctx['root']}  ({rep['mode']} checks)")
    if ctx["sha"]:
        print(f"  commit {ctx['sha'][:7]}  {ctx['date']}  {(ctx['subject'] or '')[:50]}")
    print()
    for g in rep["gates"]:
        flag = "ok " if g["ok"] else "FAIL"
        print(f"  [gate {flag}] {g['name']}: {g['detail']}")
    if rep["capped"]:
        print("\n  A gate failed, so this milestone scores 0.\n\nSCORE: 0/100")
        return
    print()
    results = {b["name"]: b for b in rep["blocks"] + rep["skipped"]}
    for name in BLOCK_NAMES:
        b = results.get(name)
        if not b:
            continue
        pts = f"{b['points']:>2}" if "points" in b else "--"
        print(f"  [ {pts}/25 ] {name}")
        print(f"            {b['detail']}")
    for n in rep["notes"]:
        print(f"  note: {n}")
    if rep["mode"] == "static":
        zeros = [b["name"] for b in rep["blocks"]]
        if zeros:
            print(f"\nSTATIC: {len(zeros)} block(s) already fail from the files alone. "
                  f"Fix them, then run with --kind (or push and let your PR job run).")
        else:
            print("\nSTATIC: all four blocks pass their static checks. The live checks decide "
                  "the score: run with --kind, or push and let your PR job run.")
    elif rep["skipped"]:
        print(f"\nSCORE: {rep['score']}/{25 * len(rep['blocks'])} checked "
              f"({len(rep['skipped'])} block(s) NOT CHECKED - run it again)")
    else:
        print(f"\nSCORE: {rep['score']}/100")


def exit_code(rep):
    if rep["capped"]:
        return 1
    if rep["mode"] == "static":
        return 1 if rep["blocks"] else 0
    return 0 if rep["score"] == 100 and not rep["skipped"] else 1


# ================================================================ main

def commit_info(repo_root, rev="HEAD"):
    rc, out, _ = run(["git", "log", "-1", "--format=%H%x09%cI%x09%s", rev], cwd=repo_root)
    if rc != 0 or not out.strip():
        return "", "", ""
    return tuple((out.strip().split("\t") + ["", "", ""])[:3])


def deadline_commit(repo_root):
    rc, out, _ = run(["git", "rev-list", "-1", "--first-parent", f"--before={DEADLINE}", "HEAD"],
                     cwd=repo_root)
    return out.strip() if rc == 0 and out.strip() else None


def main(argv=None, clock=None, rng=None):
    ap = argparse.ArgumentParser(description="Mark INF 345 milestone 4.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="https://github.com/<user>/<project> - cloned fresh")
    src.add_argument("--path", help="a local checkout (marks the files as they are on disk)")
    live = ap.add_mutually_exclusive_group()
    live.add_argument("--kind", action="store_true",
                      help=f"create a throwaway kind cluster ({KIND_CLUSTER}) for the live checks")
    live.add_argument("--kind-cluster", metavar="NAME",
                      help="use an existing kind cluster for the live checks")
    ap.add_argument("--keep-cluster", action="store_true", help="do not delete the cluster made by --kind")
    ap.add_argument("--at-deadline", action="store_true",
                    help=f"mark the last commit on or before {DEADLINE}")
    ap.add_argument("--json", action="store_true", help="print the result as JSON only")
    ap.add_argument("--json-out", metavar="FILE", help="also write the result as JSON to FILE")
    a = ap.parse_args(argv)

    live_mode = bool(a.kind or a.kind_cluster)
    if live_mode:
        missing = [t for t in ("kubectl", "kind", "docker") if not shutil.which(t)]
        if missing:
            print(f"::error::the live checks need {', '.join(missing)} on PATH")
            return 2

    temps = []
    try:
        if a.repo:
            slug = repo_slug(a.repo)
            tmp = tempfile.mkdtemp(prefix="m4-")
            temps.append(tmp)
            rc, _, err = run(["git", "clone", "--quiet", a.repo, tmp], timeout=300)
            if rc != 0:
                print(f"::error::could not clone {a.repo}: {short(err, 200)}")
                return 1
            repo_root = root = tmp
        else:
            repo_root = root = os.path.abspath(a.path)
            rc, out, _ = run(["git", "remote", "get-url", "origin"], cwd=repo_root)
            slug = repo_slug(out.strip()) if rc == 0 else None
        sha, date, subject = commit_info(repo_root)
        notes = []
        if a.at_deadline:
            dc = deadline_commit(repo_root)
            if dc:
                sha, date, subject = commit_info(repo_root, dc)
                notes.append(f"marking the last commit on or before {DEADLINE}")
                if a.repo:
                    run(["git", "checkout", "--quiet", "--detach", dc], cwd=repo_root)
                else:                               # never touch a student's working tree
                    root = tempfile.mkdtemp(prefix="m4-tree-")
                    temps.append(root)
                    p1 = subprocess.run(["git", "archive", dc], cwd=repo_root, capture_output=True)
                    subprocess.run(["tar", "-x", "-C", root], input=p1.stdout, capture_output=True)
            else:
                notes.append(f"no commit on or before {DEADLINE} - marking the newest commit")
        ctx = build_ctx(root, repo_root, slug, sha, date, subject)
        ctx["notes"].extend(notes)
        cluster = kind = None
        name = a.kind_cluster or KIND_CLUSTER
        if live_mode:
            kind = Kind()
            if a.kind_cluster and not kind.exists(a.kind_cluster):
                print(f"::error::there is no kind cluster called {a.kind_cluster} (kind get clusters)")
                return 2
            cluster = Cluster(f"kind-{name}", clock=clock)
        rep = mark(ctx, live_mode, cluster=cluster, kind=kind, cluster_name=name,
                   create=bool(a.kind), keep=a.keep_cluster, clock=clock, rng=rng)
        rep["checked_out_of"] = 25 * len(rep["blocks"]) if not rep["capped"] else 100
        if a.json_out:
            with open(a.json_out, "w", encoding="utf-8") as f:
                json.dump(rep, f, indent=2)
        if a.json:
            print(json.dumps(rep, indent=2))
        else:
            print_report(ctx, rep)
        return exit_code(rep)
    finally:
        for t in temps:
            shutil.rmtree(t, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
