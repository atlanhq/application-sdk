# 1. Checking whether the charts present have sub-charts, if yes, updating them.
# 2. Finally creating a helm package for every chart

echo "Linting the chart for Secure Agent Components"
helm dependency update
helm lint ./atlanhq/charts/atlan

echo "Linting the chart for Dapr Shared Instance"
helm dependency update
helm lint ./atlanhq/charts/app

echo "Linting the chart for Secure Agent App"
helm dependency update
helm lint ./atlanhq/charts/app
