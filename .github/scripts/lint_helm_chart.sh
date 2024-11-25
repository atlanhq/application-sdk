# 1. Checking whether the charts present have sub-charts, if yes, updating them.
# 2. Finally creating a helm package for every chart

echo "Linting the chart for secure agent components"
helm dependency update
helm lint ./atlan_charts/secure_agent_components

echo "Linting the chart for secure agent app"
helm dependency update
helm lint ./atlan_charts/secure_agent_apps
