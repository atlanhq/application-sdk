# 1. Checking whether the charts present have sub-charts, if yes, updating them.
# 2. Finally creating a helm package for every chart

echo "Linting the chart for secure agent components"
cd ./atlan_charts/secure_agent_components
helm dependency update
helm lint

cd ../../

echo "Linting the chart for secure agent app"
cd ./atlan_charts/secure_agent_apps
helm dependency update
helm lint
