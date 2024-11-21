# 1. Checking whether the charts present have sub-charts, if yes, updating them.
# 2. Finally creating a helm package for every chart

cd ./charts

helm dependency update
helm lint
