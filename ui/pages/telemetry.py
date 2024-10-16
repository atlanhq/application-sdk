import json

from dash import html, dcc, callback, Output, Input
import dash
import pandas as pd
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import plotly.express as px

from sqlalchemy import create_engine

dash.register_page(__name__)

engine = create_engine("sqlite:////tmp/app.db")

logs_df = pd.read_sql("""
    SELECT 
        severity,
        observed_timestamp,
        body,
        JSON_EXTRACT(attributes, '$.workflow_id') as workflow_id,
        JSON_EXTRACT(attributes, '$.run_id') as run_id,
        JSON_EXTRACT(attributes, '$.activity_id') as activity_id,
        attributes,
        trace_id,
        span_id 
    FROM logs
    ORDER BY observed_timestamp DESC
""", con=engine)

traces_df = pd.read_sql_table("traces", con=engine)
metrics_df = pd.read_sql_table("metrics", con=engine)
sums_metrics_df = pd.read_sql(
    """
    SELECT 
        name,
        description,
        datetime(CAST(json_extract(data_points, '$.sum.startTimeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as start_time,
        datetime(CAST(json_extract(data_points, '$.sum.timeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as end_time,
        CAST(json_extract(data_points, '$.sum.asInt') AS INTEGER) as value
    FROM metrics
    WHERE json_extract(data_points, '$.sum') IS NOT NULL
    ORDER BY start_time DESC
    """, con=engine)

histogram_metrics_df = pd.read_sql(
"""
SELECT
    name,
    description,
    datetime(CAST(json_extract(data_points, '$.histogram.startTimeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as start_time,
    datetime(CAST(json_extract(data_points, '$.histogram.timeUnixNano') AS float) / 1e9, 'unixepoch', 'localtime') as end_time,
    CAST(json_extract(data_points, '$.histogram.count') AS INTEGER) as count,
    CAST(json_extract(data_points, '$.histogram.sum') AS FLOAT) as sum,
    CAST(json_extract(data_points, '$.histogram.min') AS FLOAT) as min,
    CAST(json_extract(data_points, '$.histogram.max') AS FLOAT) as max,
    json_extract(data_points, '$.histogram.bucketCounts') as bucketCounts,
    json_extract(data_points, '$.histogram.explicitBounds') as explicitBounds
FROM metrics
WHERE json_extract(data_points, '$.histogram') IS NOT NULL
ORDER BY start_time DESC
""", con=engine)



layout = html.Div([
    dbc.Container([
        html.H1('Telemetry Dashboard'),
        dbc.Card(
            [
                dbc.CardHeader(
                    dbc.Tabs(
                        [
                            dbc.Tab(label="Logs", tab_id="logs-tab"),
                            dbc.Tab(label="Traces", tab_id="traces-tab"),
                            dbc.Tab(label="Metrics", tab_id="metrics-tab"),
                        ],
                        id="card-tabs",
                        active_tab="logs-tab",
                    )
                ),
                dbc.CardBody(html.P(id="card-content", className="card-text")),
            ]
        )],
        fluid=True
    )
])


def return_ag_grid(df, table_id):
    return dbc.Container([
        dag.AgGrid(
            id=table_id,
            columnDefs=[{"field": i} for i in df.columns],
            rowData=df.to_dict("records"),
            columnSize="sizeToFit",
            defaultColDef={
                "filter": True
            },
            dashGridOptions={
                "pagination": True, "animateRows": True,
                "tooltipShowDelay": 0, "tooltipHideDelay": 2000
            },
            className="ag-theme-balham compact dbc-ag-grid",
        )],
        fluid=True,
        className="dbc"
    )

def sum_each_index(list_of_lists):
  """Sums the elements at each index across all sublists."""

  result = []
  if not list_of_lists:
    return result

  max_length = max(len(sublist) for sublist in list_of_lists)

  for i in range(max_length):
    index_sum = 0
    for sublist in list_of_lists:
      if i < len(sublist):
        index_sum += int(sublist[i])
    result.append(index_sum)

  return result

@callback(
    Output("card-content", "children"),
    [
        Input("card-tabs", "active_tab")
    ]
)
def render_content(active_tab):
    if active_tab == 'logs-tab':
        return return_ag_grid(logs_df, 'logs-table')
    elif active_tab == 'traces-tab':
        return return_ag_grid(traces_df, 'traces-table')
    elif active_tab == 'metrics-tab':
        # sums_df = metrics_df.filter()
        # Get all the metric names in the df
        metric_names = sums_metrics_df['name'].unique()
        data = []
        for sum_metric_name in metric_names:
            df = sums_metrics_df[sums_metrics_df['name'] == sum_metric_name]
            metric_description = df['description'].iloc[0]
            card = dbc.Card([
                dbc.CardHeader(sum_metric_name),
                dbc.CardBody([
                    html.P(metric_description),
                    dcc.Graph(
                        id=f"{sum_metric_name}-graph",
                        figure=px.line(
                            df,
                            x="start_time",
                            y="value",
                        )
                    )
                ])
            ])
            data.append(card)

        # Get all the histogram metric names in the df
        histogram_metric_names = histogram_metrics_df['name'].unique()
        for histogram_metric_name in histogram_metric_names:
            df = histogram_metrics_df[histogram_metrics_df['name'] == histogram_metric_name]
            metric_description = df['description'].iloc[0]

            # aggregate data
            bucket_counts = df['bucketCounts'].apply(lambda x: json.loads(x)).tolist()
            explicit_bounds = df['explicitBounds'].apply(lambda x: json.loads(x)).tolist()

            agg_bucket_counts = sum_each_index(bucket_counts)
            card = dbc.Card([
                dbc.CardHeader(histogram_metric_name),
                dbc.CardBody([
                    html.P(metric_description),
                    dcc.Graph(
                        id=f"{histogram_metric_name}-graph",
                        figure=px.bar(
                            x=explicit_bounds[0],
                            y=agg_bucket_counts[1:],
                            labels={"x": "Bucket Bounds", "y": "Count"}
                        )
                    )
                ])
            ])
            data.append(card)
        return data
