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
        for metric_name in metric_names:
            df = sums_metrics_df[sums_metrics_df['name'] == metric_name]
            metric_description = df['description'].iloc[0]
            card = dbc.Card([
                dbc.CardHeader(metric_name),
                dbc.CardBody([
                    html.P(metric_description),
                    dcc.Graph(
                        id=f"{metric_name}-graph",
                        figure=px.line(
                            df,
                            x="start_time",
                            y="value",
                        )
                    )
                ])
            ])
            data.append(card)
        return data
