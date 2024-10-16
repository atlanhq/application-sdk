from dash import html, dcc, callback, Output, Input
import dash
import pandas as pd
import dash_ag_grid as dag
import dash_bootstrap_components as dbc

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
        return return_ag_grid(metrics_df, 'metrics-table')
