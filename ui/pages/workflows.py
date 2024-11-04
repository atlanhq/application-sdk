import dash
import dash_bootstrap_components as dbc
import dash_dangerously_set_inner_html
import pandas as pd
import pygwalker as pyg
from dash import callback, html
from dash.dependencies import Input, Output

from ui.pages.interfaces.utils import create_ag_grid
from ui.pages.interfaces.workflow import WorkflowInterface

dash.register_page(__name__, name="🏗️ Workflows")

workflow_interface = WorkflowInterface()

layout = html.Div(
    [
        dbc.Container(
            [
                html.H1("🏗️ Workflows Dashboard"),
                dbc.Row(
                    [
                        dbc.Col(
                            [
                                dbc.Card(
                                    [
                                        dbc.CardHeader("Workflow List"),
                                        dbc.CardBody(
                                            [
                                                dbc.Button(
                                                    "Refresh",
                                                    id="refresh-button",
                                                    class_name="icon-refresh",
                                                ),
                                                create_ag_grid(
                                                    grid_id="workflows-table",
                                                    row_df=None,
                                                ),
                                            ]
                                        ),
                                    ]
                                ),
                            ],
                            width=6,
                        ),
                        dbc.Col(
                            [
                                dbc.Card(
                                    [
                                        dbc.CardHeader("Workflow Logs"),
                                        dbc.CardBody(
                                            [
                                                create_ag_grid(
                                                    grid_id="workflows-logs",
                                                    row_df=None,
                                                )
                                            ]
                                        ),
                                    ]
                                )
                            ],
                            width=6,
                        ),
                    ]
                ),
                dbc.Row(
                    [
                        html.H2("Workflow Analysis"),
                        dbc.Col(
                            [
                                dbc.Card(
                                    [
                                        dbc.CardHeader("Output Files"),
                                        dbc.CardBody(
                                            [
                                                create_ag_grid(
                                                    grid_id="output-files",
                                                    row_df=None,
                                                )
                                            ]
                                        ),
                                    ]
                                )
                            ],
                            width=4,
                        ),
                        dbc.Col(
                            [
                                dbc.Card(
                                    [
                                        dbc.CardHeader("Analysis"),
                                        dbc.CardBody([html.Div(id="file-analysis")]),
                                    ]
                                )
                            ],
                            width=8,
                        ),
                    ]
                ),
                dbc.Row(
                    [
                        html.H2("Workflow History"),
                        dbc.Col(
                            [
                                dbc.Card(
                                    [
                                        dbc.CardHeader("Workflow History"),
                                        dbc.CardBody(
                                            [
                                                create_ag_grid(
                                                    grid_id="history-table",
                                                    row_df=None,
                                                )
                                            ]
                                        ),
                                    ]
                                ),
                            ],
                            width=12,
                        ),
                    ]
                ),
            ],
            fluid=True,
            className="dbc",
        ),
    ]
)


@callback(
    [
        Output("workflows-table", "rowData"),
        Output("workflows-table", "columnDefs"),
    ],
    [Input("refresh-button", "n_clicks")],
)
def refresh_workflows_table(n_clicks):
    df, column_defs = workflow_interface.fetch_workflows_df()
    return df.to_dict("records"), column_defs


@callback(
    [Output("workflows-logs", "rowData"), Output("workflows-logs", "columnDefs")],
    [Input("workflows-table", "selectedRows")],
    prevent_initial_call=True,
)
def show_workflow_logs(selected_rows):
    if not selected_rows:
        return [], []
    workflow_id, run_id = workflow_interface.fetch_workflow_info(selected_rows)
    df = workflow_interface.get_workflow_logs_df(workflow_id, run_id)
    return df.to_dict("records"), [{"field": i} for i in df.columns]


@callback(
    [
        Output("history-table", "rowData"),
        Output("history-table", "columnDefs"),
    ],
    [Input("workflows-table", "selectedRows")],
    prevent_initial_call=True,
)
def show_workflow_events(selected_rows):
    if not selected_rows:
        return [], []
    workflow_id, run_id = workflow_interface.fetch_workflow_info(selected_rows)
    df = workflow_interface.fetch_workflow_events_df(workflow_id, run_id)
    return df.to_dict("records"), [{"field": i} for i in df.columns]


@callback(
    Output("output-files", "rowData"),
    Output("output-files", "columnDefs"),
    [Input("workflows-table", "selectedRows")],
    prevent_initial_call=True,
)
def file_listing(selected_rows):
    if not selected_rows:
        return [], []

    workflow_id, run_id = workflow_interface.fetch_workflow_info(selected_rows)
    files_df = workflow_interface.fetch_files_df(workflow_id, run_id)
    return files_df.to_dict("records"), [{"field": i} for i in files_df.columns]


@callback(
    Output("file-analysis", "children"),
    [Input("output-files", "selectedRows")],
    prevent_initial_call=True,
)
def debug_file(selected_rows):
    if not selected_rows:
        return []
    selected_file = selected_rows[0]
    if not selected_file["filename"].endswith(".json"):
        print("Not a JSON file")
        return

    df = None
    try:
        df = pd.read_json(selected_rows[0]["full_path"], lines=True)
    except Exception as e:
        print(e)
        pass

    if df is None:
        df = workflow_interface.read_json_file(selected_file["full_path"])

    walker = pyg.walk(df, debug=False)
    html_code = walker.to_html()
    return dash_dangerously_set_inner_html.DangerouslySetInnerHTML(html_code)
