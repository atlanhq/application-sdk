# 1. Checking whether the charts present have sub-charts, if yes, updating them.
# 2. Finally creating a helm package for every chart

echo "Linting the chart for secure agent components"
cd ./charts/secure-agent-components
helm dependency update
helm lint

cd ../../

echo "Linting the chart for secure agent app"
cd ./charts/secure-agent-app
helm dependency update
helm lint
