#!/usr/bin/env bash
# INF 345 Week 9 - the incident drill. Your lecturer gives you a number from 1 to 6.
#
#     bash drill/start.sh <number>      deploy the "drill" service, with ONE thing broken
#     bash drill/start.sh check         call the service the way a user would
#     bash drill/start.sh reset         delete everything the drill made
#
# Please do not read the rest of this file before you have found the fault: that is
# like reading the answer. Find it the way you would at 3 a.m. - get, describe, logs,
# events - fix it on the cluster, then write your five-line postmortem.
set -euo pipefail

NS=drill
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="$(tr -d '[:space:]' < "$HERE/IMAGE")"

case "${1:-}" in
  check)
    kubectl -n "$NS" run check --rm -i --restart=Never --quiet \
      --image=curlimages/curl:8.22.0 -- curl -sS --max-time 5 http://drill/healthz \
      && echo "   <- the service answers" || echo "   <- the service does NOT answer"
    exit 0 ;;
  reset)
    kubectl delete namespace "$NS" --ignore-not-found
    exit 0 ;;
  [1-6]) FAULT="$1" ;;
  *) echo "usage: bash drill/start.sh <1-6 | check | reset>" >&2; exit 1 ;;
esac

if ! [[ "$IMAGE" =~ ^ghcr\.io/[a-z0-9._/-]+:[0-9a-f]{40}$ ]]; then
  echo "drill/IMAGE does not name an image yet: '$IMAGE'" >&2
  echo "Your lecturer fills it in before the lab. Pull the newest version: git pull" >&2
  exit 1
fi

# The healthy values...
TAG_IMAGE="$IMAGE"; SELECT="drill"; READY_PORT="http"; CM_KEY="PORT"
LIVE_PATH="/healthz"; CPU="50m"
# ...and the one thing that is broken today.
case "$FAULT" in
  1) TAG_IMAGE="${IMAGE%:*}:$(echo "${IMAGE##*:}" | cut -c1-7)" ;;
  2) SELECT="drill-app" ;;
  3) READY_PORT="8081" ;;
  4) CM_KEY="port" ;;
  5) LIVE_PATH="/health" ;;
  6) CPU="50" ;;
esac

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" create secret generic app-secret \
  --from-literal=API_KEY="$(openssl rand -hex 16 2>/dev/null || date +%s%N)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n "$NS" apply -f - >/dev/null <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: drill-config
data:
  $CM_KEY: "8080"
  GREETING: "Salem"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: drill
spec:
  replicas: 2
  selector:
    matchLabels:
      app: drill
  template:
    metadata:
      labels:
        app: drill
    spec:
      securityContext:
        runAsNonRoot: true
      containers:
        - name: app
          image: $TAG_IMAGE
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: PORT
              valueFrom:
                configMapKeyRef:
                  name: drill-config
                  key: PORT
            - name: GREETING
              valueFrom:
                configMapKeyRef:
                  name: drill-config
                  key: GREETING
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: app-secret
                  key: API_KEY
          readinessProbe:
            httpGet:
              path: /healthz
              port: $READY_PORT
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: $LIVE_PATH
              port: http
            periodSeconds: 5
            failureThreshold: 3
          resources:
            requests:
              cpu: "$CPU"
              memory: 64Mi
            limits:
              cpu: "$CPU"
              memory: 128Mi
---
apiVersion: v1
kind: Service
metadata:
  name: drill
spec:
  selector:
    app: $SELECT
  ports:
    - port: 80
      targetPort: http
EOF

echo "The drill service is deployed in namespace '$NS'. Users say it does not work."
echo "Start with:  kubectl -n $NS get pods      and   bash drill/start.sh check"
echo "Then write your postmortem: what users saw, how you found it (the command and the"
echo "line), the root cause, the fix, and how to prevent it next time."
