{{/*
App name (required).
*/}}
{{- define "atlan-app.appName" -}}
{{- required "appName is required" .Values.appName -}}
{{- end -}}

{{/*
App module (required).
*/}}
{{- define "atlan-app.appModule" -}}
{{- required "appModule is required" .Values.appModule -}}
{{- end -}}

{{/*
Namespace: namespaceOverride or app-{appName}.
*/}}
{{- define "atlan-app.namespace" -}}
{{- if .Values.namespaceOverride -}}
  {{- .Values.namespaceOverride -}}
{{- else -}}
  {{- printf "app-%s" (include "atlan-app.appName" .) -}}
{{- end -}}
{{- end -}}

{{/*
Container image: {registry}/{repository}:{tag}.
Repository defaults to application-sdk/{appName}.
*/}}
{{- define "atlan-app.image" -}}
{{- $repo := .Values.image.repository | default (printf "application-sdk/%s" (include "atlan-app.appName" .)) -}}
{{- printf "%s/%s:%s" .Values.image.registry $repo .Values.image.tag -}}
{{- end -}}

{{/*
Task queue name.
*/}}
{{- define "atlan-app.taskQueue" -}}
{{- printf "%s-queue" (include "atlan-app.appName" .) -}}
{{- end -}}

{{/*
Service account name.
*/}}
{{- define "atlan-app.serviceAccountName" -}}
{{- printf "%s-sa" (include "atlan-app.appName" .) -}}
{{- end -}}

{{/*
ConfigMap name.
*/}}
{{- define "atlan-app.configMapName" -}}
{{- printf "%s-config" (include "atlan-app.appName" .) -}}
{{- end -}}

{{/*
Handler deployment name.
*/}}
{{- define "atlan-app.handlerName" -}}
{{- printf "%s-handler" (include "atlan-app.appName" .) -}}
{{- end -}}

{{/*
Worker deployment name.
*/}}
{{- define "atlan-app.workerName" -}}
{{- printf "%s-worker" (include "atlan-app.appName" .) -}}
{{- end -}}

{{/*
Chart label: {chartName}-{chartVersion}.
*/}}
{{- define "atlan-app.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" -}}
{{- end -}}

{{/*
Common labels applied to all resources.
*/}}
{{- define "atlan-app.commonLabels" -}}
app: {{ include "atlan-app.appName" . }}
app.kubernetes.io/name: {{ include "atlan-app.appName" . }}
app.kubernetes.io/part-of: application-sdk
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ include "atlan-app.chart" . }}
{{- end -}}

{{/*
Handler selector labels.
*/}}
{{- define "atlan-app.handlerSelectorLabels" -}}
app: {{ include "atlan-app.appName" . }}
component: handler
{{- end -}}

{{/*
Worker selector labels.
*/}}
{{- define "atlan-app.workerSelectorLabels" -}}
app: {{ include "atlan-app.appName" . }}
component: worker
{{- end -}}
