# INF 345 — Week 9 lab: an incident drill, then M4

**Tuesday 10 November · Group A 12:30 · Group B 13:30 · on your own laptop**

**M4 is due Sunday 15 November, 23:59** (Almaty time, judged by commit time). The marker,
`mark_m4.py`, is in this repository and it is the rubric: run it on yourself as often as you
like.

## Tonight: before the lab (15 minutes)

1. Your Week 8 cluster works: `kubectl get nodes` says `Ready`. No cluster? Use Killercoda or
   a Codespace, as in Week 8.
2. Week 8's Step 6 is done: your own image answered `/healthz` on your cluster. M4 starts
   from there.
3. Your M3 package is **public**, and you know the full 40-character SHA of a commit whose
   image exists (`git log -1 --format=%H` on `main`, then check the package page).
4. Download the marker into your project (it needs Python 3 and PyYAML, as for M3). Do not
   commit it: add `mark_m4.py` to `.git/info/exclude`.

   ```bash
   cd ~/<your-project>
   curl -fsSLO https://raw.githubusercontent.com/inf345/inf345-week9-lab/main/mark_m4.py
   python3 mark_m4.py --help
   ```

5. Clone this repository next to your project:

   ```bash
   git clone https://github.com/inf345/inf345-week9-lab.git ~/inf345-week9-lab
   ```

---

## In the lab, part 1: the incident drill (15 minutes)

You are on call and users are complaining. I give you a number from 1 to 6.

```bash
cd ~/inf345-week9-lab
bash drill/start.sh <your number>      # deploys the "drill" service with ONE thing broken
bash drill/start.sh check              # calls it the way a user would
```

Find the fault **without reading `drill/start.sh`**:

```bash
kubectl -n drill get pods
kubectl -n drill describe pod <a name>          # Events at the bottom
kubectl -n drill logs <a name>                  # add --previous after a crash
kubectl -n drill get events --sort-by=.lastTimestamp
kubectl -n drill get endpointslices
```

Fix it on the cluster with `kubectl -n drill edit deployment drill` (or `service drill`, or
`configmap drill-config` followed by `kubectl -n drill rollout restart deployment drill`).
Run `bash drill/start.sh check` until it answers `ok`.

Then paste a five-line postmortem in the chat:

```
Impact:      what users saw
Detection:   the command, and the exact line that showed the problem
Root cause:  what was wrong
Fix:         what you changed
Prevention:  what would stop it next time (a check, a review rule, a test)
```

Blame the system, not a person: "the manifest had a typo and nothing checked it". When you
are done: `bash drill/start.sh reset`.

---

## In the lab, part 2: M4, your service on Kubernetes

**What `kubectl apply -f k8s/` must deploy.** A `k8s/` folder in your project with plain
YAML files (no subfolders, no Helm):

- **exactly one Deployment** running **your M3 image by its full commit SHA**, with **2 or
  more replicas** and `runAsNonRoot: true`;
- **a Service** that selects your Pods and sends traffic to your `PORT`;
- **a ConfigMap** that gives the container `PORT` and **one more setting** your app really
  reads;
- **one value from the Secret `app-secret`**;
- a **readinessProbe** and a **livenessProbe**: `httpGet` on `/healthz`, on your `PORT`;
- **CPU and memory requests and limits**.

> **Secrets never go into Git, not even base64.** Your Deployment reads from a Secret
> with the fixed name `app-secret` (`secretKeyRef: name: app-secret, key: <you choose>`).
> `k8s/` contains **no `kind: Secret`**: the marker treats one as a leaked credential. On
> your cluster you create it yourself; the marker creates it with a new random value.

### Step 1: copy the starter and fill 25 blanks (20 min)

```bash
cd ~/<your-project>
git switch main && git pull && git switch -c m4
mkdir -p k8s
cp ~/inf345-week9-lab/starter/k8s/*.yaml k8s/
git rm -q --cached k8s/app.yaml 2>/dev/null; rm -f k8s/app.yaml   # Week 8's file, if you copied it
```

Fill every `____` in the three files; the comment next to each blank is the question.

- **Blank 9**, the second setting: something your app really uses (a greeting, a log
  level). If it reads none yet, add one line such as `os.environ.get("GREETING", "hello")`.
  I may ask where your app uses it.
- **Blank 15**: readiness must notice before liveness acts. `periodSeconds ×
  failureThreshold` is 5 × 3 = 15 s for readiness, so liveness needs more, for example
  10 × 3 = 30 s.

### Step 2: run it on your cluster (10 min)

```bash
kubectl create secret generic app-secret --from-literal=API_KEY="$(openssl rand -hex 16)"
kubectl apply --dry-run=server -f k8s/
kubectl apply -f k8s/
kubectl rollout status deployment/<blank 1> --timeout=180s
kubectl port-forward service/<blank 23> 8080:80
```

Use your own key from blank 10 instead of `API_KEY`. In another terminal:
`curl -i localhost:8080/healthz` should give `200`.

**Apple M-series Mac:** your cluster cannot run an amd64 image (`no match for platform in
manifest`). Use a Codespace or Killercoda, or go to Step 4B.

### Step 3: the static checks (5 min)

```bash
python3 mark_m4.py --path .
```

Fix until it says `STATIC: all four blocks pass their static checks.`

### Step 4: the live checks (20 min, some of it waiting)

The score comes from the live checks: the marker applies your `k8s/` to a real cluster and
watches it. Two ways:

- **A. Intel or AMD laptop:** `python3 mark_m4.py --path . --kind`. It makes its own
  throwaway cluster and deletes it after; your Week 8 cluster is not touched.
- **B. A pull-request job (works for everyone, Apple M-series too):** copy
  `examples/k8s-pr.yml` to `.github/workflows/k8s.yml` in your project, fill its four
  blanks, push your `m4` branch and open a pull request.

You are done when you see `SCORE: 100/100`.

### Step 5: merge before Sunday 23:59

The marker marks the last commit on your default branch before **Sunday 15 November,
23:59**: `k8s/` as it is in that commit, with the image named in it. That image must exist
and be public.

---

## How M4 is marked

**Gates (any failure means 0):** `k8s/` parses and has only allowed kinds; every container
runs your own image (or the course's rescue image); nothing unsafe (`hostPath`,
`privileged`, host networking, added capabilities); no Secret or secret value in `k8s/`; no
credential in your Git history; the API server accepts every file in a server-side dry run.

| Block, 25 points each | From your files | Live, on a cluster |
|---|---|---|
| it deploys | your image by a commit SHA of your repository, 2+ replicas, `runAsNonRoot` | every Pod ready within 180 s |
| it serves | a Service selects your Pods and reaches your `PORT` | all Pods are ready endpoints; `/healthz` answers 5 of 5 through the Service |
| the probes are real | `httpGet /healthz` on your `PORT`, sane timings | one Pod is **frozen**: readiness takes it out, liveness restarts it |
| configured, not hardcoded | `PORT` and one more setting from the ConfigMap, a value from `app-secret`, requests and limits | a **random `PORT`** is written into your ConfigMap and it must still answer |

A block scores only if every check in it passes. **NOT CHECKED** means the marker's cluster
had a problem; it is not a zero, and I re-run it. If your own image is broken, the rescue
image in `drill/IMAGE` costs only the 25 points of *it deploys*. In the Tuesday sessions I
may ask you about one line of your `k8s/`. Late work: 10 points per started day, down to 50.

<details>
<summary><b>The limits behind "sane timings", and what the gates allow</b></summary>

- Allowed kinds: Deployment, Service, ConfigMap, ServiceAccount, PodDisruptionBudget,
  HorizontalPodAutoscaler, NetworkPolicy. Namespace, Gateway, HTTPRoute and Ingress are read
  but not applied. Exactly one Deployment, at least one Service.
- Probes: `periodSeconds × failureThreshold` at most 60 s for readiness and 120 s for
  liveness; `initialDelaySeconds` at most 60; grace period at most 60 s.
- The official run uses this `mark_m4.py` with `--kind --at-deadline` on GitHub's Intel
  machines (Kubernetes 1.37.0, kind v0.33.0).
- Why a `tcpSocket` probe fails the freeze test: the kernel still accepts connections for
  a frozen program, so only `httpGet` notices.

</details>

---

## Stuck?

1. `kubectl get pods` → `kubectl describe pod <name>` (Events at the bottom) →
   `kubectl logs <name>` (add `--previous` after a crash).
2. `python3 mark_m4.py --path .` names the block and the problem.
3. Look up the exact words from Events in *Common failures* below.
4. In the session, paste the error **text** in the chat. After it, email me from your
   `@sdu.edu.kz` address (subject: `INF 345` and your group) with the link to your pull
   request, or the output of `mark_m4.py --path .` and `kubectl describe pod`, as text.

<details>
<summary><b>Common failures</b></summary>

| You see | Fix |
|---|---|
| `ImagePullBackOff`, `not found` | The tag is wrong: a short SHA, a typo, or a commit whose run never published. Copy the full SHA again |
| `ImagePullBackOff`, `401 Unauthorized` or `failed to fetch anonymous token` | The package is private: Package settings → Change visibility → Public |
| `ImagePullBackOff`, `no match for platform in manifest` | Apple M-series cluster, amd64 image: use Step 4B |
| `CreateContainerConfigError`, `secret "app-secret" not found` | Create it: `kubectl create secret generic app-secret --from-literal=<KEY>="$(openssl rand -hex 16)"` |
| `CreateContainerConfigError`, `couldn't find key … in Secret` (or `in ConfigMap`) | The key in your Deployment and the key you created differ. Keys are case-sensitive |
| `CreateContainerConfigError`, `image has non-numeric user (app)` | Your Dockerfile says `USER app`. Use `USER 1000:1000` (Week 5), rebuild through CI, use the new SHA |
| `CreateContainerConfigError`, `image will run as root` | Your Dockerfile has no `USER` line: add `USER 1000:1000` |
| `Running` but `READY 0/1`, `Readiness probe failed: … connection refused` | Nothing listens on that port: blank 7, blanks 12/14 and `PORT` must agree, and the app must listen on `0.0.0.0` |
| `RESTARTS` growing, `Liveness probe failed: … statuscode: 404` | The liveness probe calls a path your app does not have: use `/healthz` |
| `OOMKilled`, exit code 137 | Over the memory limit: raise `limits.memory` (Java: see below) |
| `Pending`, `Insufficient cpu` (or `memory`) | Your requests are too big: 50m and 64Mi are plenty |
| `… must be less than or equal to cpu limit` | A request is bigger than its limit |
| `strict decoding error: unknown field "…livenesProbe"` | A misspelled field: fix the spelling |
| The marker says `a Secret in Git is a leaked secret` | `git rm` the Secret file and commit. If it ever held a real value, change that value everywhere |
| You changed the ConfigMap and the app still uses the old value | Environment variables are read at start: `kubectl rollout restart deployment/<name>` |
| `namespaces "…" not found` | Remove `metadata.namespace` from your files; choose it with `-n` when you apply |
| The PR job fails with `couldn't find key API_KEY in Secret` | Blank 4 of the job (`SECRET_KEY`) must be the key your Deployment reads |

</details>

<details>
<summary><b>Your language</b></summary>

| Language | Resources that work | Watch out for |
|---|---|---|
| Python | requests 50m / 64Mi, limits 250m / 128Mi | nothing special; the `/tmp` volume covers writes |
| Node | requests 50m / 64Mi, limits 250m / 192Mi | run `node server.js`, not `npm start` (npm wants to write to your home folder) |
| Go | requests 20m / 32Mi, limits 200m / 64Mi | distroless has no shell: `kubectl logs` is your window |
| Java | requests 100m / 256Mi, limits 1 / 512Mi | add a `startupProbe` (`httpGet /healthz`, `periodSeconds: 2`, `failureThreshold: 30`); the JVM sizes its heap from the memory limit |

The security lines that are already filled (`seccompProfile`, `allowPrivilegeEscalation`,
`readOnlyRootFilesystem` with a writable `/tmp`, `drop: ["ALL"]`) meet Kubernetes'
"restricted" level. Your app needs none of root's powers.

</details>

<details>
<summary><b>Stretch goals (not marked)</b></summary>

**1. Traffic from outside, with the Gateway API**

```bash
kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/v1.9.2/install.yaml
kubectl wait --timeout=5m -n envoy-gateway-system deployment/envoy-gateway --for=condition=Available
```

Fill the one blank in `gateway/gateway.yaml` (your Service's name), then:

```bash
kubectl apply -f ~/inf345-week9-lab/gateway/gateway.yaml
kubectl get gateway,httproute
ENVOY=$(kubectl get svc -n envoy-gateway-system \
  -l gateway.envoyproxy.io/owning-gateway-name=eg -o jsonpath='{.items[0].metadata.name}')
kubectl -n envoy-gateway-system port-forward "service/$ENVOY" 8888:80
```

In another terminal: `curl -i -H "Host: mine.local" localhost:8888/healthz`. Keep the file
out of `k8s/`.

**2. Change a setting, the right way.** Change your second setting in `configmap.yaml`,
apply, and check what the app sees with `kubectl exec deploy/<name> -- printenv <SETTING>`.
Nothing changed until `kubectl rollout restart deployment/<name>`.

**3. Keep one Pod alive during maintenance.** Add a `PodDisruptionBudget` with
`minAvailable: 1`, then `kubectl drain` a node of a three-node kind cluster (Week 8,
stretch goal 1).

**4. The strictest security level.** `kubectl label namespace default
pod-security.kubernetes.io/warn=restricted`, then apply `k8s/` again: no warning means the
starter's security lines are all there.

**5. Freeze your own Pod** (Intel/AMD kind cluster), the marker's probe test by hand:

```bash
POD=$(kubectl get pods -l app=<your label> -o jsonpath='{.items[0].metadata.name}')
CID=$(kubectl get pod "$POD" -o jsonpath='{.status.containerStatuses[0].containerID}' | sed 's#.*://##')
PID=$(docker exec inf345-control-plane crictl inspect -o go-template --template '{{.info.pid}}' "$CID")
docker exec inf345-control-plane kill -STOP "$PID"
kubectl get endpointslices -w        # your Pod leaves the Service...
kubectl get pods -w                  # ...and liveness restarts it
```

</details>
