## Streamlit-based Observability UI

The Application SDK includes a Streamlit-based observability UI that provides interactive visualizations for logs, metrics, and traces. This UI is optional and can be enabled by setting the `ATLAN_ENABLE_STREAMLIT_BASED_OBSERVABILITY` environment variable to `true`.

### Features

1. **Logs Visualization**
   - Stacked bar chart showing log levels over time
   - Interactive filtering and exploration of log data
   - Real-time updates as new logs are collected

2. **Metrics Visualization**
   - Counter metrics: Line charts showing cumulative counts over time
   - Histogram metrics: Distribution plots and time series of mean values
   - Interactive exploration of metric data

3. **Traces Visualization**
   - Time series visualization of trace data
   - Interactive exploration of trace patterns

### Setup

1. Install the required dependencies:
   ```bash
   uv sync --extra observability
   ```

2. Enable the Streamlit UI in your environment:
   ```bash
   export ATLAN_ENABLE_STREAMLIT_BASED_OBSERVABILITY=true
   ```

3. The UI will be available at `http://localhost:8501` by default.

### Dependencies

The Streamlit UI requires the following optional dependencies:
- `streamlit>=1.32.0`: For the web UI framework
- `plotly>=5.19.0`: For interactive visualizations

These dependencies are included in the `observability` optional dependency group.

### Configuration

| Environment Variable | Description | Default Value |
|---------------------|-------------|---------------|
| `ATLAN_ENABLE_STREAMLIT_BASED_OBSERVABILITY` | Whether to enable the Streamlit-based observability UI | `false` | 